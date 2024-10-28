import os
import numpy as np
import matplotlib.pyplot as plt
import tensorflow as tf
import scenario_pb2

plt.figure(figsize=(30, 30))
plt.rcParams['axes.facecolor'] = 'grey'

# 选择交互车辆
data_path = './validation'
file_list = os.listdir(data_path)

for cnt_file, file in enumerate(file_list):
    file_path = os.path.join(data_path, file)
    dataset = tf.data.TFRecordDataset(file_path, compression_type='')
    for cnt_scenar, data in enumerate(dataset):
        scenario = scenario_pb2.Scenario()
        scenario.ParseFromString(bytearray(data.numpy()))
        print(scenario.scenario_id)

        # 画地图线
        for i in range(len(scenario.map_features)):
            # 车道线
            if str(scenario.map_features[i].lane) != '':
                line_x = [z.x for z in scenario.map_features[i].lane.polyline]
                line_y = [z.y for z in scenario.map_features[i].lane.polyline]
                plt.scatter(line_x, line_y, c='g', s=5)
            # plt.text(line_x[0], line_y[0], str(scenario.map_features[i].id), fontdict={'family': 'serif', 'size': 20, 'color': 'black'})
            # 边界线
            if str(scenario.map_features[i].road_edge) != '':
                road_edge_x = [polyline.x for polyline in scenario.map_features[i].road_edge.polyline]
                road_edge_y = [polyline.y for polyline in scenario.map_features[i].road_edge.polyline]
                plt.scatter(road_edge_x, road_edge_y)
                # plt.text(road_edge_x[0], road_edge_y[0], scenario.map_features[i].road_edge.type, fontdict={'family': 'serif', 'size': 20, 'color': 'black'})
                if scenario.map_features[i].road_edge.type == 2:
                    plt.scatter(road_edge_x, road_edge_y, c='k')

                elif scenario.map_features[i].road_edge.type == 3:
                    plt.scatter(road_edge_x, road_edge_y, c='purple')
                    print(scenario.map_features[i].road_edge)
                else:
                    plt.scatter(road_edge_x, road_edge_y, c='k')
            # 道路边界线
            if str(scenario.map_features[i].road_line) != '':
                road_line_x = [j.x for j in scenario.map_features[i].road_line.polyline]
                road_line_y = [j.y for j in scenario.map_features[i].road_line.polyline]
                if scenario.map_features[i].road_line.type == 7:  # 双实黄线
                    plt.plot(road_line_x, road_line_y, c='y')
                elif scenario.map_features[i].road_line.type == 8:  # 双虚实黄线
                    plt.plot(road_line_x, road_line_y, c='y')
                elif scenario.map_features[i].road_line.type == 6:  # 单实黄线
                    plt.plot(road_line_x, road_line_y, c='y')
                elif scenario.map_features[i].road_line.type == 1:  # 单虚白线
                    for i in range(int(len(road_line_x) / 7)):
                        plt.plot(road_line_x[i * 7:5 + i * 7], road_line_y[i * 7:5 + i * 7], color='w')
                elif scenario.map_features[i].road_line.type == 2:  # 单实白线
                    plt.plot(road_line_x, road_line_y, c='w')
                else:
                    plt.plot(road_line_x, road_line_y, c='w')

        # 画车及轨迹
        for i in range(len(scenario.tracks)):
            if i == scenario.sdc_track_index:
                traj_x = [center.center_x for center in scenario.tracks[i].states if center.center_x != 0.0]
                traj_y = [center.center_y for center in scenario.tracks[i].states if center.center_y != 0.0]
                head = [center.heading for center in scenario.tracks[i].states if center.center_y != 0.0]
                plt.scatter(traj_x[0], traj_y[0], s=140, c='r', marker='s')
                # plt.imshow(img1,extent=[traj_x[0]-3, traj_x[0]+3,traj_y[0]-1.5, traj_y[0]+1.5])
                plt.scatter(traj_x, traj_y, s=14, c='r')
            else:
                traj_x = [center.center_x for center in scenario.tracks[i].states if center.center_x != 0.0]
                traj_y = [center.center_y for center in scenario.tracks[i].states if center.center_y != 0.0]
                head = [center.heading for center in scenario.tracks[i].states if center.center_y != 0.0]
                plt.scatter(traj_x[0], traj_y[0], s=140, c='k', marker='s')
                # plt.imshow(img1,extent=[traj_x[0]-3, traj_x[0]+3,traj_y[0]-1.5, traj_y[0]+1.5])
                plt.scatter(traj_x, traj_y, s=14, c='b')
        # break
        plt.show()
    break
plt.show()
