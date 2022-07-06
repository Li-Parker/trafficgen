import numpy as np
import logging
import torch
import torch.nn as nn
import math


from drivingforce.TrafficTranformer.models.utils import Encoder, EncoderLayer, Decoder, DecoderLayer, \
    MultiHeadAttention, PointerwiseFeedforward, MLP_FFN, PE, LocalSubGraph,MCG_block,MLP,MLP_3,CG_stacked
import copy
logger = logging.getLogger(__name__)
copy_func = copy.deepcopy
version = 0

_Z = 1024
_H = _Z
_S = 32  # sequence length
STATE_NORM_FACTOR = 25.  # maximum typical goal distance, meters

class TransformerLWMConf(object):
    adam_eps = 1e-05
    adam_lr = 0.0003
    amp = True
    vehicle_dim = 8

    n_embd = 512
    image_size = 64
    image_channels = 3
    vecobs_size = 2
    n_action = 3
    # optimizer
    grad_clip = 200
    # transformer block params
    block_size = 31 # sequence length
    embd_pdrop = 0.1
    resid_pdrop = 0.1
    attn_pdrop = 0.1
    n_layer = 8
    n_head = 8

class TrafficFormerDynamic(nn.Module):
    """ A transformer model with wider latent space """

    def __init__(self):
        super().__init__()

        # input embedding stem
        hidden_dim = 1024
        self.CG_agent = CG_stacked(5,hidden_dim)
        self.CG_line = CG_stacked(5, hidden_dim)
        self.CG_all = CG_stacked(5, hidden_dim*2)
        self.agent_encode = MLP_3([8,256,512,hidden_dim])
        self.line_encode = MLP_3([5,256,512,hidden_dim])
        self.type_embedding = nn.Embedding(20, hidden_dim)
        self.traf_embedding = nn.Embedding(4, hidden_dim)
        self.anchor_embedding = nn.Embedding(6, hidden_dim*2)
        # self.anchor_embedding.weight.requires_grad == False
        # nn.init.orthogonal_(self.anchor_embedding.weight)
        #
        self.apply(self._init_weights)
        # prob,long_perc,lat_perc,dir(2),v_value,v_dir = 1+1+1+2+1+2
        self.pred_len = 44
        self.speed_head = MLP_3([hidden_dim*2,hidden_dim,256,self.pred_len])
        self.angle_head = MLP_3([hidden_dim * 2, hidden_dim, 256, self.pred_len])
        self.prob_head = MLP_3([hidden_dim * 2, hidden_dim, 256, 1])
        self.pos_head = MLP_3([hidden_dim*2,hidden_dim,256,self.pred_len*2])

    def _init_weights(self, module):
        if isinstance(module, (nn.Linear, nn.Embedding)):
            module.weight.data.normal_(mean=0.0, std=0.02)
            if isinstance(module, nn.Linear) and module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


    def forward(self, data, is_training=True):

        agent = data['agent']
        agent_mask = data['agent_mask']


        all_vec = torch.cat([data['center'],data['cross'],data['bound']],dim=-2)
        line_mask = torch.cat([data['center_mask'],data['cross_mask'],data['bound_mask']],dim=1)

        polyline = all_vec[...,:5]
        polyline_type = all_vec[...,5].to(int)
        polyline_traf = all_vec[..., 6].to(int)
        polyline_type_embed = self.type_embedding(polyline_type)
        polyline_traf_embed = self.traf_embedding(polyline_traf)

        agent_enc = self.agent_encode(agent)
        line_enc = self.line_encode(polyline) + polyline_traf_embed + polyline_type_embed
        b,a,d = agent_enc.shape

        device = agent_enc.device

        context_agent = torch.ones([b,d]).to(device)
        agent_enc, context_agent = self.CG_agent(agent_enc, context_agent,agent_mask)

        line_enc, context_line= self.CG_line(line_enc, context_agent, line_mask)

        all_context = torch.cat([context_agent,context_line],dim=-1)

        anchors = self.anchor_embedding.weight.unsqueeze(0).repeat(b,1,1)
        mask = torch.ones(*anchors.shape[:-1]).to(device)
        pred, _ = self.CG_all(anchors,all_context,mask)

        prob = (self.prob_head(pred)).squeeze(-1)
        speed_pred = nn.ReLU()(self.speed_head(pred)).view(b,6,self.pred_len)
        pos = self.pos_head(pred).view(b,6,self.pred_len,2)
        heading_diff_pred = self.angle_head(pred).view(b,6,self.pred_len)
        #output = self.output_head(pred).view(b,6,self.pred_len,2)

        # x = torch.cumsum(output[..., 0], dim=-1).unsqueeze(-1)
        # y = torch.cumsum(output[..., 1], dim=-1).unsqueeze(-1)
        # pos = torch.cat([x, y], dim=-1)

        heading = heading_diff_pred.cumsum(-1)
        # x = torch.cumsum(speed_pred*torch.sin(heading),dim=-1).unsqueeze(-1)
        # y = torch.cumsum(speed_pred*torch.cos(heading),dim=-1).unsqueeze(-1)
        # pos = torch.cat([x, y], dim=-1)

        if is_training:

            MSE = torch.nn.MSELoss(reduction='none')
            L1 = torch.nn.L1Loss(reduction='none')
            CLS = torch.nn.CrossEntropyLoss()

            gt = data['ego_gt'][..., :2].unsqueeze(1).repeat(1,6,1,1)
            speed_gt = data['processed_gt']['speed'].unsqueeze(1).repeat(1,6,1)
            heading_gt = data['processed_gt']['heading'].unsqueeze(1).repeat(1,6,1)

            pred_end = pos[:,:,-1]
            gt_end = gt[:,:,-1]
            dist = MSE(pred_end,gt_end).mean(-1)
            min_index = torch.argmin(dist, dim=-1)

            pos_loss = MSE(pos,gt[:,:,1:]).mean(-1).mean(-1)

            speed_loss = L1(speed_gt,speed_pred).mean(-1)
            speed_loss = torch.gather(speed_loss,dim=1,index=min_index.unsqueeze(-1)).mean()

            heading_loss = L1(heading_gt,heading_diff_pred).mean(-1)
            heading_loss = torch.gather(heading_loss,dim=1,index=min_index.unsqueeze(-1)).mean()

            pos_loss = torch.gather(pos_loss,dim=1,index=min_index.unsqueeze(-1)).mean()

            cls_loss = CLS(prob,min_index)

            losses = {}
            losses['cls_loss'] =cls_loss
            losses['speed_loss'] = speed_loss
            losses['heading_loss'] = heading_loss
            losses['pos_loss'] = pos_loss
            #losses['v_dir_loss'] = v_dir_loss

            total_loss = cls_loss+heading_loss+speed_loss+10*pos_loss

        if is_training:
            return pos, prob, total_loss,losses
        else:
            ret = torch.cat([pos,speed_pred.unsqueeze(-1),heading.unsqueeze(-1)],dim=-1)
            return ret, prob
