import copy
import numpy as np
from shapely.geometry import Polygon
import pickle

from tqdm import tqdm
from random import choices,seed
import time
import torch
from torch import Tensor
import torch.distributed as dist
import wandb
from torch import optim
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data import DataLoader

from utils.utils import get_time_str,time_me,transform_to_agent,from_list_to_batch,rotate,get_type_class
from utils.typedef import AgentType

from TrafficGen_init.models.init_model import initializer
from TrafficGen_init.data_process.init_dataset import initDataset

from utils.visual_init import get_agent_pos_from_vec,draw,draw_heatmap,get_heatmap
from utils.visual_act import draw_seq
from TrafficGen_init.data_process.agent_process import WaymoScene

from TrafficGen_act.models.act_model import actuator
from TrafficGen_act.data_process.act_dataset import actDataset,process_case_to_input,process_map

import os

os.environ['KMP_DUPLICATE_LIB_OK'] = 'True'


class Trainer:
    def __init__(self,
                 save_freq=1,
                 exp_name=None,
                 wandb_log_freq=100,
                 cfg=None,
                 args=None
                 ):
        self.args = args
        self.cfg = cfg

        self.model_type = cfg['model']

        if args.distributed:
            torch.distributed.init_process_group(backend="nccl", rank=args.local_rank)
            args.world_size = torch.distributed.get_world_size()
            args.rank = torch.distributed.get_rank()
            print('[TORCH] Training in distributed mode. Process %d, local %d, total %d.' % (
                args.rank, args.local_rank, args.world_size))

        if self.model_type == 'init':
            train_dataset = initDataset(self.cfg, args)
            model = initializer()
        elif self.model_type == 'act':
            train_dataset = actDataset(self.cfg, args)
            model = actuator()
        else:
            raise NotImplementedError('no such model!')
        if len(train_dataset)>0:
            data_loader = DataLoader(train_dataset, batch_size=cfg['batch_size'],
                                     shuffle=True,
                                     num_workers=self.cfg['num_workers'])
            self.train_dataloader = data_loader
        if args.distributed:
            model.cuda(args.local_rank)
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
            model = DistributedDataParallel(model,
                                            device_ids=[args.local_rank],
                                            output_device=args.local_rank,
                                            broadcast_buffers=False)  # only single machine
        else:
            model = torch.nn.DataParallel(model, list(range(1)))
            model = model.to(self.cfg['device'])

        optimizer = optim.AdamW(model.parameters(), lr=self.cfg['lr'], betas=(0.9, 0.999), eps=1e-09,
                                weight_decay=self.cfg['weight_decay'], amsgrad=True)

        self.eval_data_loader = None

        if self.main_process and self.cfg["need_eval"]:
            test_set = initDataset(self.cfg, args, eval=True) if self.model_type=='init' else actDataset(self.cfg, args, eval=True)
            self.eval_data_loader = DataLoader(test_set, shuffle=False, batch_size=cfg['eval_batch_size'],
                                               num_workers=self.cfg['num_workers'])

        self.model = model
        self.in_debug = cfg["debug"]  # if in debug, wandb will log to TEST instead of cvpr
        self.optimizer = optimizer

        self.batch_size = cfg['batch_size']
        self.max_epoch = cfg['max_epoch']
        self.current_epoch = 0
        self.save_freq = save_freq
        self.exp_name = "v2_{}_{}".format(exp_name, get_time_str()) if exp_name is not None else "v2_{}".format(
            get_time_str())
        self.exp_data_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exps",
                                          self.exp_name)
        self.wandb_log_freq = wandb_log_freq
        self.total_sgd_count = 1
        self.training_start_time = time.time()
        #self.mmd_loss = MMD_loss()
        if self.main_process and not self.in_debug:
            self.make_dir()
            wandb.login(key="a6ae178e5596edd2aa7e54d4d34abebe5759406e")
            wandb.init(
                entity="drivingforce",
                project="cvpr" if not self.in_debug else "TEST",
                name=self.exp_name,
                config=cfg)
            gpu_num = torch.cuda.device_count()
            print("gpu number:{}".format(gpu_num))
            print("gpu available:", torch.cuda.is_available())
            # wandb.watch(model) # gradient stat

    def train(self):
        while self.current_epoch < self.max_epoch:
            epoch_start_time = time.time()
            current_sgd_count = self.total_sgd_count
            train_data = self.train_dataloader
            _, epoch_training_time = self.train_one_epoch(train_data)
            # log data in epoch level
            if self.main_process:

                wandb.log({
                    "epoch_training_time (s)": time.time() - epoch_start_time,
                    "sgd_iters_in_this_epoch": self.total_sgd_count - current_sgd_count,
                    "epoch": self.current_epoch,
                    "epoch_training_time": epoch_training_time,
                })


                if self.current_epoch % self.cfg["eval_frequency"] == 0:
                    self.save_model()
                    if self.cfg["need_eval"]:
                        self.eval_model()


    def get_heatmap(self,inp):

        gt_num = 4

        inp['agent_mask'][:,gt_num:] = 0
        pred, _, _ = self.model(inp,False)
        gt_idx = inp['vec_index'][:,:gt_num]
        plt = draw_heatmap(inp['center'][0],pred[0,:,0],gt_idx)

        return plt

    def process_case_for_eval(self,data_path,data_num):
        self.data_for_gen = {}
        cnt = 0
        while cnt < data_num:
            data_file_path = os.path.join(data_path, f'{cnt}.pkl')

            with open(data_file_path, 'rb+') as f:
                datas = pickle.load(f)

            self.data_for_gen[cnt] = self.process(datas)

            cnt+=1

    def transform_coordinate_map(self,data,lane):
        """
        Every frame is different
        """
        pos = data['sdc_pos'][0]
        lane[..., :2] -= pos
        sdc_theta = data['sdc_theta'][0]
        x = lane[..., 0]
        y = lane[..., 1]
        x_transform = np.cos(sdc_theta) * x - np.sin(sdc_theta) * y
        y_transform = np.cos(sdc_theta) * y + np.sin(sdc_theta) * x
        output_coords = np.stack((x_transform, y_transform), axis=-1)
        lane[..., :2] = output_coords

        return lane

    def process(self,data):
        case_info = {}

        data['lane'] = self.transform_coordinate_map(data,data['lane'])
        data['unsampled_lane'] = self.transform_coordinate_map(data, data['unsampled_lane'])
        # case_info

        scene = WaymoScene(data)
        # agent: x,y,vx,vy,l,w,sin_head,cos_head
        case_info["agent"] = scene.agent_data
        case_info['agent'][..., 4] = 5.286
        case_info['agent'][..., 5] = 2.332
        case_info["agent_mask"] = scene.agent_mask

        case_info['center'], case_info['center_mask'], case_info['bound'], case_info['bound_mask'], \
        case_info['cross'], case_info['cross_mask'], case_info['rest'] = self.process_map(data)

        self.filter_agent(case_info)

        vec_ind = case_info['vec_index'].to(int)
        line_seg = Tensor(case_info['center'])

        # agent info in world coord[:8] and vector coord[8:]
        padding_content = torch.cat([case_info['agent'], case_info['long_perc'].unsqueeze(-1),
                                     case_info['lat_perc'].unsqueeze(-1), case_info['relative_dir'].unsqueeze(-1),
                                     case_info['v_value'].unsqueeze(-1)], dim=-1)

        gather_vec = vec_ind.view(*vec_ind.shape, 1).repeat(1, 1, 7)
        the_vec = torch.gather(line_seg, 1, gather_vec)
        line_with_agent = torch.cat([padding_content, the_vec], -1)

        b, v, _ = line_seg.shape
        gt = torch.zeros([b, v, 5])
        padding_content = padding_content[..., 8:]
        agent_mask = case_info["agent_mask"]
        for i in range(b):
            mask = agent_mask[i]
            agent_info = padding_content[i]
            indx = vec_ind[i]
            indx = indx[mask.to(bool)]
            gt[i, indx, 0] = 1
            gt[i, indx, 1:] = agent_info[mask.to(bool)]

        case_info['gt'] = gt
        case_info['line_with_agent'] = line_with_agent

        ind = list(range(0, 190, 10))
        lane = data['lane'][ind]
        case_info['lane'] = lane
        case_list = []

        for i in range(1):
            dic = {}
            for k, v in case_info.items():
                dic[k] = v[i]
            case_list.append(dic)

        pos = data['sdc_pos'][0]
        lane = data['unsampled_lane']
        lane[..., :2] -= pos
        sdc_theta = data['sdc_theta'][0]
        x = lane[..., 0]
        y = lane[..., 1]
        x_transform = np.cos(sdc_theta) * x - np.sin(sdc_theta) * y
        y_transform = np.cos(sdc_theta) * y + np.sin(sdc_theta) * x
        output_coords = np.stack((x_transform, y_transform), axis=-1)
        lane[..., :2] = output_coords

        case_list[0]['traf'] = data['traf_p_c_f']
        case_list[0]['center_info'] = data['center_info']
        case_list[0]['unsampled_lane'] = lane
        return case_list

    def draw_generation_process(self,vis=True, save=False):
        save_path = '/Users/fenglan/Downloads/waymo/onemap'
        self.model.eval()
        with torch.no_grad():
            eval_data = self.eval_data_loader.dataset
            cnt = 0

            #for k in range(20):
            for i in tqdm(range(len(eval_data))):
                seed(i)
                batch = copy.deepcopy(eval_data[i])

                for key in batch.keys():
                    if isinstance(batch[key], np.ndarray):
                        batch[key] = Tensor(batch[key])
                    if isinstance(batch[key], torch.Tensor) and self.cfg['device'] == 'cuda':
                        batch[key] = batch[key].cuda()
                cnt+=1
                inp = copy.deepcopy(batch)

                other = inp['other']
                inp_lane = {}
                inp_lane['traf'] = other['traf'][0]
                inp_lane['lane'] = other['unsampled_lane']
                cent, cent_mask, bound, bound_mask, _, _, rest = process_map(inp_lane, 4000, 1000, 50,0)

                pred_agent, prob,coords = self.inference(inp)
                agent_num = pred_agent.shape[0]

                #heat_maps = []
                if vis:
                    if not os.path.exists('./vis/heatmap'):
                        os.mkdir('./vis/heatmap')
                    path = f'./vis/heatmap/{i}'
                    if not os.path.exists(path):
                        os.mkdir(path)
                    for j in range(1,agent_num):
                        output_path = os.path.join(path,f'{j}')
                        draw_agent = pred_agent[:j]
                        center = inp['center'][0].numpy()
                        center = cent
                        center_mask = inp['center_mask'][0].numpy().astype(bool)
                        heat_map = get_heatmap(coords[j-1][:,0], coords[j-1][:,1], prob[j-1][center_mask], 20)
                        #heat_maps.append(heat_map)
                        draw(center, heat_map,draw_agent,rest, edge=bound,save=True,path=output_path)

                # generate case
                if save:
                    for key in batch.keys():
                        if isinstance(batch[key], torch.Tensor):
                            batch[key] = batch[key].cpu().numpy()
                    output = {}
                    case_agent = np.zeros([pred_agent.shape[0],9])
                    case_agent[:,-2:]=1
                    case_agent[:,:4] = pred_agent[:,:4]
                    case_agent[:,5:7] = pred_agent[:,4:6]
                    case_agent[:,4] = -np.arctan2(pred_agent[:,6],pred_agent[:,7])+np.pi/2
                    #output['lane'] = batch['lane'][0]
                    output['all_agent'] = case_agent
                    output['other'] = batch['other']
                    output['lane'] = batch['other']['lane']
                    output['other'].pop('lane')
                    output['gt_agent'] = inp['agent'][0].numpy()
                    #output['heat_map'] = heat_maps

                    p = os.path.join(save_path, f'{i}.pkl')
                    with open(p, 'wb') as f:
                        pickle.dump(output, f)


    def eval_model(self):

        self.model.eval()
        ret = {}
        loss = 0
        losses = []


        with torch.no_grad():
            eval_data = self.eval_data_loader
            cnt = 0

            for batch in eval_data:
                for key in batch.keys():
                    if isinstance(batch[key], torch.DoubleTensor):
                        batch[key] = batch[key].float()
                    if isinstance(batch[key], torch.Tensor) and self.cfg['device'] == 'cuda':
                        batch[key] = batch[key].cuda()

                inp_batch = copy.deepcopy(batch)
                pred, loss_, losses_ = self.model(inp_batch)

                loss+=loss_
                losses.append(losses_)

                i = 0
                if cnt <10:
                    a_case = {}
                    for key in batch.keys():
                        if isinstance(batch[key], torch.Tensor):
                            batch[key] = batch[key].cpu()
                        a_case[key] = batch[key][[i]]

                    inp = copy.deepcopy(a_case)
                    pred_agent = self.inference(inp)
                    agent_num = pred_agent.shape[0]
                    plt_pred = wandb.Image(draw(a_case['center'][0],pred_agent,edge=a_case['bound'][0]))

                    plt_gt = wandb.Image(draw(a_case['center'][0],a_case['agent'][0,:agent_num],a_case['bound'][0]))
                    ret[f"pred_{cnt}"] = plt_pred
                    ret[f'gt_{cnt}'] = plt_gt

                    ret[f'heatmap_{cnt}'] = wandb.Image(self.get_heatmap(copy.deepcopy(a_case)))

                cnt += 1

        loss = loss/cnt
        dic = {}
        for i in range(len(losses)):
            lo = losses[i]
            for k,v in lo.items():
                if i == 0:
                    dic[k] = v
                else:
                    dic[k] += v
        for k,v in dic.items():
            dic[k] = (v/cnt)

        dic['loss'] = loss
        wandb.log({'eval':dic})
        wandb.log(ret)

    def get_polygon(self, center, yaw, L, W):
        # agent = WaymoAgentInfo(agent)
        #yaw = yaw.cpu()

        l, w = L / 2, W / 2
        theta = np.arctan(l / w)
        s1 = np.sqrt(l ** 2 + w ** 2)
        x1 = abs(np.cos(theta + yaw) * s1)
        y1 = abs(np.sin(theta + yaw) * s1)
        x2 = abs(np.cos(theta - yaw) * s1)
        y2 = abs(np.sin(theta - yaw) * s1)

        p1 = [center[0] + x1, center[1] - y1]
        p2 = [center[0] - x1, center[1] + y1]
        p3 = [center[0] + x2, center[1] + y2]
        p4 = [center[0] - x2, center[1] - y2]
        return Polygon([p1, p3, p2, p4])

    def inference(self, data):

        agent_num = data['agent_mask'].sum().to(int)

        idx_list = []
        vec_indx = data['vec_index'].cpu().numpy().astype(int)
        idx_list.append(vec_indx[0,0])
        shapes = []
        ego_poly = self.get_polygon(data['line_with_agent'][0,0,:2].cpu().numpy(),
                                    np.arctan2(data['line_with_agent'][0,0,6].cpu().numpy(),data['line_with_agent'][0,0,7].cpu().numpy()),
                                    5.286 + 0.1, 2.332 + 0.1)
        shapes.append(ego_poly)
        prob_list = []
        minimum_agent = 4
        center = data['center'][0].numpy()
        center_mask = data['center_mask'][0].numpy().astype(bool)
        center = center[center_mask]
        coord_list = []
        for i in range(1,max(agent_num,minimum_agent)):
            data['agent_mask'][:, :i] = 1
            data['agent_mask'][:,i:]=0

            pred, _, _ = self.model(data, False)

            #pred_coord = np.zeros_like(center)
            vec = center[:,:4]
            the_pred = pred[0,:vec.shape[0]].numpy()
            coord, agent_dir, vel = get_agent_pos_from_vec(vec, the_pred[:, 1], the_pred[:, 2], the_pred[:, 3],
                                                       the_pred[:, 4])
            coord_list.append(coord)
            # for i in range(center.shape[0]):
            #     vec = center[i,:4]
            #     the_pred = pred[0,i]
            #     coord, agent_dir, vel = get_agent_pos_from_vec(vec, the_pred[:, 1], the_pred[:, 2], the_pred[:, 3],
            #                                                the_pred[:, 4])
            #     pred_coord[i,:4] = coord

            pred = pred.cpu().numpy()

            prob = pred[0,:,0]
            prob[idx_list] = 0
            line_mask = data['center_mask'][0].cpu().numpy()
            prob = prob*line_mask

            # loop 100 times for collision detection

            for j in range(100):
                sample_list = []
                # repeat sampling
                for k in range(1):
                    indx = choices(list(range(prob.shape[-1])), prob)[0]
                    sample_list.append((indx,prob[indx]))
                max_prob = np.argmax([x[1] for x in sample_list])
                indx = sample_list[max_prob][0]
                vec = data['center'][:,indx].cpu().numpy()
                the_pred = pred[:,indx]
                coord, agent_dir,vel = get_agent_pos_from_vec(vec,the_pred[:,1],the_pred[:,2],the_pred[:,3],the_pred[:,4])

                intersect = False
                poly = self.get_polygon(coord[0], np.arctan2(agent_dir[0,0],agent_dir[0,1]), 5.286 + 0.2, 2.332 + 0.2)
                for shape in shapes:
                    if poly.intersects(shape):
                        intersect = True
                        break
                if not intersect:
                    shapes.append(poly)
                    break
                else: continue

            prob_list.append(prob)
            idx_list.append(indx)
            data['line_with_agent'][:,i,:2] = Tensor(coord)
            data['line_with_agent'][:, i, 2:4] = Tensor(vel)
            data['line_with_agent'][:, i, 4:6] = Tensor([5.286,2.332])
            data['line_with_agent'][:, i, 6:8] = Tensor(agent_dir)
            data['line_with_agent'][:, i,8:12] = Tensor(the_pred[:,1:])
            data['line_with_agent'][:, i, 12:] = Tensor(vec)

        agent = data['line_with_agent'][0,:max(agent_num,minimum_agent),:8]
        return agent.cpu().numpy(), prob_list,coord_list
    @time_me
    def train_one_epoch(self, train_data):
        self.current_epoch += 1
        self.model.train()
        sgd_count = 0

        for data in train_data:
            # Checking data preprocess
            for key in data.keys():
                if isinstance(data[key], torch.DoubleTensor):
                    data[key] = data[key].float()
                if isinstance(data[key], torch.Tensor) and self.cfg['device'] == 'cuda':
                    data[key] = data[key].cuda()
            self.optimizer.zero_grad()

            pred, loss,losses = self.model(data)

            loss_info = {}
            for k,v in losses.items():
                loss_info[k] = self.gather_loss_stat(v)
            loss_info['loss'] = self.gather_loss_stat(loss)

            loss.backward()
            self.optimizer.step()
            # log data
            if self.main_process:
                # log data in batch level
                if sgd_count % self.wandb_log_freq == 0:
                    self.print_log(loss_info)
                    self.print("\n")
                    wandb.log(loss_info)
                # * Display the results.
            sgd_count += 1
            self.total_sgd_count += 1

    def draw_gifs(self,vis=True,save_path=None):
        self.model.eval()
        with torch.no_grad():
            eval_data = self.eval_data_loader

            scene_data = eval_data.dataset.scene_data
            pred_list = []

            for i in tqdm(range(len(scene_data))):
                data_inp = scene_data[i]
                #heat_map = data_inp['heat_map']
                pred_i = self.inference_control(data_inp)
                pred_i['pred']=np.delete(pred_i['pred'],4,axis=1)
                pred_list.append(pred_i)

                if vis:
                    other = scene_data[i]['other']
                    lane = pred_i['lane']
                    traf = other['traf']
                    agent = pred_i['pred']
                    dir_path = f'./vis/gif/{i}'
                    if not os.path.exists(dir_path):
                        os.mkdir(dir_path)
                    ind = list(range(0,190,5))
                    #agent = np.delete(agent,4,axis=1)
                    #del heat_map[5]
                    agent = agent[ind]
                    for t in range(agent.shape[0]):
                    #for t in [0, 20, 40, 60, 80, 100, 120, 140, 160, 180]:
                        agent_t = agent[t]
                        #path = os.path.join(dir_path,f'{t+agent_t.shape[0]-1}')
                        path = os.path.join(dir_path, f'{t}')
                        traf_t = traf[t]
                        inp = {}
                        inp['traf'] = traf_t
                        inp['lane'] = lane
                        cent,cent_mask,bound,bound_mask,_,_,rest = process_map(inp, 2000, 1000, 70, 0)
                        #cent,cent_mask,bound,bound_mask,_,_,rest = WaymoDataset.process_map(inp,2000,1000,100)
                        # if t==0:
                        #     center, _, bounder, _, _, _, rester = WaymoDataset.process_map(inp, 2000, 1000, 50,0)
                        #     for k in range(1,agent_t.shape[0]):
                        #         heat_path = os.path.join(dir_path, f'{k-1}')
                        #         draw(cent, heat_map[k-1], agent_t[:k], rest, edge=bound, save=True, path=heat_path)
                        draw_seq(cent,bound,rest,agent_t,path=path,save=True)

            if save_path:
                self.save_as_metadrive_data(pred_list,scene_data,save_path)

    def inference_control(self, data, length = 190, per_time = 10):
        # for every x time step, pred then update
        future_traj = data['other']['agent_traj']
        pred_agent = np.zeros([length,*data['all_agent'].shape])
        pred_agent[0] = copy.deepcopy(data['all_agent'])
        # vel = (pred_agent[0,:,2]**2 + pred_agent[0,:,3]**2)**0.5
        # pred_agent[0][vel<0.05,2:4]=0
        pred_agent[1:,:,5:] = pred_agent[0,:,5:]

        #ind = list(range(0,190,5))
        pred_agent[:,0] = future_traj[0]
        agent_num = data['all_agent'].shape[0]

        for i in range(0,length-1,per_time):

            current_agent = copy.deepcopy(pred_agent[i])
            case_list = []
            for j in range(agent_num):
                a_case = {}
                a_case['agent'],a_case['lane'] = transform_to_agent(current_agent[j],current_agent,data['lane'])
                a_case['traf'] = data['other']['traf'][i]
                case_list.append(a_case)

            inp_list = []
            for case in case_list:
                inp_list.append(process_case_to_input(case))

            batch = from_list_to_batch(inp_list)
            # for inp in inp_list:
            #     draw(inp['center'], inp['agent'][inp['agent_mask'].astype(bool)][np.newaxis], context=inp['agent'][inp['agent_mask'].astype(bool)])
            for key in batch.keys():
                if isinstance(batch[key], torch.Tensor) and self.cfg['device'] == 'cuda':
                    batch[key] = batch[key].cuda()
            if self.cfg['device'] == 'cuda':
                self.model.cuda()

            pred = self.model(batch,False)
            prob = pred['prob']
            velo_pred = pred['velo']
            pos_pred = pred['pos']
            heading_pred = pred['heading']
            all_pred = torch.cat([pos_pred,velo_pred,heading_pred.unsqueeze(-1)],dim=-1)

            best_pred_idx = torch.argmax(prob,dim=-1)
            best_pred_idx = best_pred_idx.view(agent_num,1,1,1).repeat(1,1,*all_pred.shape[2:])
            best_pred = torch.gather(all_pred,dim=1,index=best_pred_idx).squeeze(1).cpu().numpy()

            ## update all the agent
            for j in range(1,agent_num):
                pred_j = best_pred[j]
                agent_j = copy.deepcopy(current_agent[j])
                center = copy.deepcopy(agent_j[:2])
                center_yaw = copy.deepcopy(agent_j[4])
                rotate_theta = -(center_yaw-np.pi/2)
                pos = rotate(pred_j[:, 0], pred_j[:, 1], -rotate_theta)
                heading = pred_j[...,-1]+center_yaw


                pos_ = np.concatenate([np.zeros([1,2]),pos],axis=0)
                vel = (pos_[1:]-pos_[:-1])/0.1

                speed = (vel[...,0] ** 2 + vel[...,1] ** 2) ** 0.5
                vx = (speed*np.cos(heading))[:,np.newaxis]
                vy = (speed*np.sin(heading))[:,np.newaxis]
                vel = rotate(pred_j[:, 2], pred_j[:, 3], -rotate_theta)

                pos = pos + center
                pad_len = pred_agent[i+1:i+per_time+1].shape[0]
                pred_agent[i+1:i+per_time+1,j,:2] = copy.deepcopy(pos[:pad_len])
                pred_agent[i+1:i + per_time+1, j, 2:4] = copy.deepcopy(vel[:pad_len])
                pred_agent[i+1:i + per_time+1, j, 4] = copy.deepcopy(heading[:pad_len])

        transformed = {}
        transformed['agent'],transformed['lane'] = transform_to_agent(pred_agent[0,0],pred_agent,data['lane'])
        transformed['traf'] = data['other']['traf'][0]
        transformed['pred'] = transformed['agent']
        transformed['agent'] = transformed['agent'][0]
        output = process_case_to_input(transformed,agent_range=300,edge_num=1000,center_num=6000)
        output['pred'] = transformed['pred']
        output['lane'] = data['lane']
        return output

    def save_model(self):
        if self.current_epoch % self.save_freq == 0 and self.main_process:
            if self.model_type=='act':
                model_name = f'act_{self.current_epoch}'
            else:
                model_name = f'init_{self.current_epoch}'

            model_save_name = os.path.join(self.exp_data_path, 'saved_models', model_name)
            state = {
                'state_dict': self.model.state_dict(),
                'optimizer': self.optimizer.state_dict(),
                'epoch': self.current_epoch
            }
            torch.save(state, model_save_name)
            self.print('\n model saved to %s' % model_save_name)

    def load_model(self, model_path,device):
        state = torch.load(model_path,map_location=torch.device(device))
        self.model.load_state_dict(state["state_dict"])
        self.optimizer.load_state_dict(state["optimizer"])
        self.current_epoch = state["epoch"]

    @staticmethod
    def reduce_mean_on_all_proc(tensor):
        nprocs = torch.distributed.get_world_size()
        rt = tensor.clone()
        dist.all_reduce(rt, op=dist.ReduceOp.SUM)
        rt /= nprocs
        return rt

    def print(self, *info):
        "Use this print instead of naive print, since we run in parallel"
        if self.main_process:
            print(*info)

    def print_log(self, log: dict):
        self.print("========== Epoch: {} ==========".format(self.current_epoch))
        for key, value in log.items():
            self.print(key, ": ", value)

    @property
    def main_process(self):
        return True if self.args.rank == 0 else False

    @property
    def distributed(self):
        return True if self.args.distributed else False

    def gather_loss_stat(self, loss):
        return loss.item() if not self.distributed else self.reduce_mean_on_all_proc(loss).item()

    def make_dir(self):
        os.makedirs(self.exp_data_path, exist_ok=False)
        os.mkdir(os.path.join(self.exp_data_path, "saved_models"))

    # def generate_case_for_dynamic(self, case_num, output_path):
    #     self.model.eval()
    #
    #     with torch.no_grad():
    #         eval_data = self.eval_data_loader.dataset
    #         cnt = 0
    #
    #         for i in tqdm(range(len(eval_data))):
    #             batch = eval_data[i]
    #
    #             for key in batch.keys():
    #                 if isinstance(batch[key], np.ndarray):
    #                     batch[key] = Tensor(batch[key])
    #                 if isinstance(batch[key], torch.Tensor) and self.cfg['device'] == 'cuda':
    #                     batch[key] = batch[key].cuda()
    #
    #             if cnt > case_num: break
    #
    #             p = os.path.join(output_path, f'{cnt}.pkl')
    #             cnt+=1
    #
    #             output = {}
    #             inp = copy.deepcopy(batch)
    #             pred_agent,_,_ = self.inference(inp)
    #             #
    #             # draw(batch['center'][0], pred_agent, edge=batch['bound'][0])
    #
    #             for key in batch.keys():
    #                 if isinstance(batch[key], torch.Tensor):
    #                     batch[key] = batch[key].cpu().numpy()
    #
    #             case_agent = np.zeros([pred_agent.shape[0],9])
    #             case_agent[:,-2:]=1
    #             case_agent[:,:4] = pred_agent[:,:4]
    #             case_agent[:,5:7] = pred_agent[:,4:6]
    #             case_agent[:,4] = -np.arctan2(pred_agent[:,6],pred_agent[:,7])+np.pi/2
    #             #output['lane'] = batch['lane'][0]
    #             output['all_agent'] = case_agent
    #             output['other'] = batch['other']
    #             output['lane'] = batch['other']['lane']
    #             output['other'].pop('lane')
    #             output['gt_agent'] = inp['agent'][0].numpy()
    #             # output['center_info'] = batch['center_info']
    #             # output['traf'] = batch['traf']
    #             # output['unsampled_lane'] = batch['unsampled_lane'][0]
    #             # for i in range(len(traf)):
    #             #     traf_t = traf[i]
    #             #     for j in range(len(traf_t)):
    #             #         traf_t[j] = traf_t[j][0].numpy()
    #
    #
    #             with open(p, 'wb') as f:
    #                 pickle.dump(output, f)

    # def generate_case_for_metadrive(self,data_path):
    #
    #     self.model.eval()
    #     with torch.no_grad():
    #         eval_data = self.eval_data_loader
    #
    #         scene_data = eval_data.dataset.scene_data
    #         pred_list = []
    #         for k,v in tqdm(scene_data.items()):
    #             pred_list.append(self.inference(v))
    #
    #
    #         for i in range(len(pred_list)):
    #             pred = pred_list[i]['pred']
    #             agent_lerp = np.zeros([185,*pred.shape[1:]])
    #             for j in range(pred.shape[0]-1):
    #                 pred_j = pred[j,:,:4]
    #                 pred_j1 = pred[j+1,:,:4]
    #                 delta = pred_j1-pred_j
    #                 dt = 1/5
    #                 for k in range(5):
    #                     agent_lerp[j*5+k,:,:4] = pred_j+dt*k*delta
    #                     agent_lerp[j*5 + k, :, 4:] = pred[j,:,4:]
    #             pred_list[i]['pred'] = agent_lerp
    #
    #         for i in range(len(pred_list)):
    #             gt_agent = scene_data[i]['gt_agent']
    #             #
    #             # invalid = [1,2,7]
    #             # pred_list[i]['pred'] = np.delete(pred_list[i]['pred'],invalid,axis=1)
    #
    #             other = scene_data[i]['other']
    #             lane = pred_list[i]['lane']
    #             traf = other['traf']
    #             agent = pred_list[i]['pred']
    #             # delete invalid agent
    #
    #             dir_path = f'./gifs/{i}'
    #             if not os.path.exists(dir_path):
    #                 os.mkdir(dir_path)
    #             #for t in range(agent.shape[0]):
    #             for t in [0,20,40,60,80,100,120,140,160,180]:
    #                 path = os.path.join(dir_path,f'{t}')
    #                 agent_t = agent[t]
    #                 traf_t = traf[t]
    #                 inp = {}
    #                 inp['traf'] = traf_t
    #                 inp['lane'] = lane
    #                 cent,cent_mask,bound,bound_mask,_,_,rest = WaymoDataset.process_map(inp,2000,1000,100)
    #                 if t==0:
    #                     draw_seq(cent,bound,rest,gt_agent,path=os.path.join(dir_path,'gt'),save=True,gt=True)
    #                 draw_seq(cent,bound,rest,agent_t,path=path,save=True)
    #
    #         cnt=0
    #         for j in tqdm(range(len(pred_list))):
    #             case = pred_list[j]
    #             center_info = scene_data[j]['other']['center_info']
    #             output = copy.deepcopy(output_temp)
    #             output['tracks']={}
    #             output['map'] = {}
    #             # extract agents
    #             agent = case['pred']
    #
    #             for i in range(agent.shape[1]):
    #                 track = {}
    #                 agent_i = agent[:,i]
    #                 track['type'] = AgentType.VEHICLE
    #                 state = np.zeros([agent_i.shape[0],10])
    #                 state[:,:2] = agent_i[:,:2]
    #                 state[:, 3] = 5.286
    #                 state[:, 4] = 2.332
    #                 state[:, 7:9] = agent_i[:,2:4]
    #                 state[:, -1] = 1
    #                 state[:, 6] = agent_i[:,4]+np.pi/2
    #                 track['state'] = state
    #                 output['tracks'][i] = track
    #
    #             # extract maps
    #             lane = scene_data[j]['other']['unsampled_lane']
    #             lane_id = np.unique(lane[..., -1]).astype(int)
    #             for id in lane_id:
    #
    #                 a_lane = {}
    #                 id_set = lane[..., -1] == id
    #                 points = lane[id_set]
    #                 polyline = np.zeros([points.shape[0],3])
    #                 line_type = points[0,-2]
    #                 polyline[:,:2] = points[:,:2]
    #                 a_lane['type'] = self.get_type_class(line_type)
    #                 a_lane['polyline'] = polyline
    #                 if id in center_info.keys():
    #                     a_lane.update(center_info[id])
    #                 output['map'][id] = a_lane
    #
    #             p = os.path.join(data_path, f'{cnt}.pkl')
    #             with open(p, 'wb') as f:
    #                 pickle.dump(output, f)
    #             cnt+=1