# 整体架构优化
之前提出的MutDrive
![img_2.png](img_2.png)


解决长尾效应的闭环自动驾驶框架
![img_1.png](img_1.png)

一种随机黑盒测试的工具（American Fuzzy Lop）架构设计：
![img.png](img.png)

# Waymo Data介绍
数据集：Maxmo:
数据特点：矢量图 ==> 可以对图像在像素级别上进行语义表示（具体的表示方法可以见后续数据集介绍）
![img_3.png](img_3.png)
参考论文：
根据一张空的map，可以在该地图上放置车辆，并且给出车辆的运动轨迹
Q:放置车辆如何保证科学性？
![img_8.png](img_8.png)
trafficgen整体框架
![img_7.png](img_7.png)
图片最小单元划分方法
![img_6.png](img_6.png)
实验结果
![img_4.png](img_4.png)
## 1. Motion
### 用途
Waymo motion数据集[1]主要用来做自动驾驶行为预测。数据集文件格式为tfrecord，而数据格式包含Scenario中，使用scenario.proto（protobuf）文件描述 。

接下来我们首先介绍Waymo motion数据集的数据格式，然后再介绍如何解析tfrecord文件来获取数据。

### Scenario
Scenario代表一个场景，也就是一段时间内的交通情况，包括自动驾驶车自身，其它的交通参与者（车辆、行人），以及交通灯在20s内的轨迹和状态，同时还包括了道路信息。也就是说Scenario是一条数据的最小单元，一个tfrecord文件包括多条Scenario。

Scenario具体的格式信息保存在scenario.proto 文件中，这个文件可以参考waymo数据集官方github仓库下载。这里简单介绍下protobuf，protobuf是一种文件格式，类似于json，xml等，它的主要好处是压缩的效率高，速度快，通常用于数据传输的序列化和反序列化，也可以用于保存文件。
```
message Scenario {
  reserved 9;

  // 场景唯一ID
  optional string scenario_id = 5;

  // 时间戳为一个数组，tracks[i].states的数量和时间戳数组大小相等
  // dynamic_map_states（红绿灯的状态）的大小也和时间戳数组大小相等
  repeated double timestamps_seconds = 1;

  // 当前时间在时间戳数组中的ID
  optional int32 current_time_index = 10;

  // 对象的追踪信息，是一个数组，每个对象中保存了一段时间内对象的位置和速度，
  // 这个时间对应上述时间戳数组的时间
  repeated Track tracks = 2;

  // 交通灯状态信息
  repeated DynamicMapState dynamic_map_states = 7;

  // 地图信息
  repeated MapFeature map_features = 8;

  // 自动驾驶车自身对应tracks数组中的ID
  optional int32 sdc_track_index = 6;

  // 有交互对象的ID列表
  repeated int32 objects_of_interest = 4;

  // 需要预测的对象列表，这只是建议train对象，不是包含所有object
  repeated RequiredPrediction tracks_to_predict = 11;

}
```
#### Track
每个track代表一段时间内对象的状态。

```
message Track {
  enum ObjectType {
    TYPE_UNSET = 0;  // 初始化状态.
    TYPE_VEHICLE = 1; // 汽车
    TYPE_PEDESTRIAN = 2; // 行人
    TYPE_CYCLIST = 3;  // 自行车
    TYPE_OTHER = 4;  // 其它
  }

  // 对象唯一ID，非负
  optional int32 id = 1;

  // 对象类型，见上面的枚举类型
  optional ObjectType object_type = 2;

  // 对象状态数组
  repeated ObjectState states = 3;
}

// ObjectState介绍如下
message ObjectState {
  // 对象的中心坐标
  optional double center_x = 2;
  optional double center_y = 3;
  optional double center_z = 4;

  // 对象的长宽高
  optional float length = 5;
  optional float width = 6;
  optional float height = 7;

  // 对象的朝向 [-pi, pi).
  optional float heading = 8;

  // 对象的速度
  optional float velocity_x = 9;
  optional float velocity_y = 10;

  // 数据是否有效
  optional bool valid = 11;
}
```
#### DynamicMapState
主要是交通灯状态，包含了带箭头的红绿灯、圆形红绿灯、闪烁的红绿灯（这里的闪烁实际上是常闪）
```
// 某一时刻的交通灯状态信息
message DynamicMapState {
  // 观测到的所有交通灯状态信息
  repeated TrafficSignalLaneState lane_states = 1;
}

// 交通灯状态信息
message TrafficSignalLaneState {
  // 交通对应的lane id
  optional int64 lane = 1;

  enum State {
    LANE_STATE_UNKNOWN = 0;  // 未知状态

    // 带箭头交通灯.
    LANE_STATE_ARROW_STOP = 1;
    LANE_STATE_ARROW_CAUTION = 2;
    LANE_STATE_ARROW_GO = 3;

    // 圆形交通灯.
    LANE_STATE_STOP = 4;
    LANE_STATE_CAUTION = 5;
    LANE_STATE_GO = 6;

    // 闪烁交通灯.
    LANE_STATE_FLASHING_STOP = 7;
    LANE_STATE_FLASHING_CAUTION = 8;
  }

  // 交通灯状态，值为上述的枚举类型.
  optional State state = 2;

  // 红绿灯的停止点
  optional MapPoint stop_point = 3;
}
```
#### MapFeature
地图对象，可以为车道，道路，道路边沿，停止标志，人行横道，减速带。地图由很多这样的对象组成。
```
message MapFeature {
  // 地图对象的唯一ID
  optional int64 id = 1;

  // Type specific data.
  oneof feature_data {
    LaneCenter lane = 3;  // 车道
    RoadLine road_line = 4;  // 道路
    RoadEdge road_edge = 5;  // 道路边沿
    StopSign stop_sign = 7;  // 停止标志
    Crosswalk crosswalk = 8;  // 人行横道
    SpeedBump speed_bump = 9;  // 减速带
  }
}
```
#### RequiredPrediction
必须预测的对象

```
message RequiredPrediction {
  // 预测的难度等级类型
  enum DifficultyLevel {
    NONE = 0;    // 无
    LEVEL_1 = 1;  // 等级1
    LEVEL_2 = 2;  // 等级2
  }

  // track对象tracks数组中的ID
  optional int32 track_index = 1;

  // 预测的难度等级
  optional DifficultyLevel difficulty = 2;
}
```
# 2. Map
Waymo motion数据集[1]主要用来做自动驾驶行为预测。前面已经介绍过它的数据格式以及如何解压数据和查看数据。接下来我们主要介绍数据集的地图格式以及它代表的含义，地图的格式保存在map.proto文件中。

下面我们就开始介绍地图格式。
## MapFeature地图特征
地图是由几种不同类型的MapFeature组成的，而MapFeature的结构如下：

```
/ 地图特征
message MapFeature {
  // 唯一ID
  optional int64 id = 1;

  // 类型
  oneof feature_data {
    LaneCenter lane = 3;   // 车道
    RoadLine road_line = 4;  // 道路
    RoadEdge road_edge = 5;  // 道路边沿
    StopSign stop_sign = 7;  // 停止标志
    Crosswalk crosswalk = 8;  // 人行横道
    SpeedBump speed_bump = 9;  // 减速带
  }
}
```
#### LaneCenter
车道，包含了限速、车道类型、车道中心采样点，前序和后继车道ID，以及左右车道边界和左右相邻车道。
```
// 1. 车道
message LaneCenter {
  // 道路限速，单位mph
  optional double speed_limit_mph = 1;

  // 道路类型
  enum LaneType {
    TYPE_UNDEFINED = 0;   // 未定义
    TYPE_FREEWAY = 1;  // 高速公路
    TYPE_SURFACE_STREET = 2;  // 地表街道
    TYPE_BIKE_LANE = 3;  // 自行车道
  }
  optional LaneType type = 2;

  // 车道插入2条车道之间则为真？
  optional bool interpolating = 3;

  // 车道采样点
  repeated MapPoint polyline = 8;

  // 前序车道IDs
  repeated int64 entry_lanes = 9 [packed = true];

  // 后继车道IDs
  repeated int64 exit_lanes = 10 [packed = true];

  // 车道左边界，车道边界类型不同会分段描述，注意一些车道没有边界（交叉口的车道）
  repeated BoundarySegment left_boundaries = 13;

  // 车道右边界
  repeated BoundarySegment right_boundaries = 14;

  // 左侧相邻车道列表，仅包括同向的车道
  repeated LaneNeighbor left_neighbors = 11;

  // 右侧相邻车道列表，仅包括同向的车道
  repeated LaneNeighbor right_neighbors = 12;
}

// 2.车道边界分段
message BoundarySegment {
  // 车道边界起始序号
  optional int32 lane_start_index = 1;

  // 车道边界终点序号.
  optional int32 lane_end_index = 2;

  // 边界的 MapFeature 的相邻边界特征 ID，可以是RoadLine和RoadEdge
  optional int64 boundary_feature_id = 3;

  // 相邻边界类型。 如果边界是道路边缘而不是道路线，这将设置为未知类型。
  optional RoadLine.RoadLineType boundary_type = 4;
}

// 3. 相邻车道
message LaneNeighbor {
  // 唯一ID
  optional int64 feature_id = 1;

  // 相邻车道？
  optional int32 self_start_index = 2;
  optional int32 self_end_index = 3;

  // 变道只能从这里开始
  optional int32 neighbor_start_index = 4;
  optional int32 neighbor_end_index = 5;

  // 相邻车道边界
  repeated BoundarySegment boundaries = 6;
}
```
#### RoadLine
车道线

```
message RoadLine {
  // 车道线类型
  enum RoadLineType {
    TYPE_UNKNOWN = 0;
    TYPE_BROKEN_SINGLE_WHITE = 1; 
    TYPE_SOLID_SINGLE_WHITE = 2;
    TYPE_SOLID_DOUBLE_WHITE = 3;
    TYPE_BROKEN_SINGLE_YELLOW = 4;
    TYPE_BROKEN_DOUBLE_YELLOW = 5;
    TYPE_SOLID_SINGLE_YELLOW = 6;
    TYPE_SOLID_DOUBLE_YELLOW = 7;
    TYPE_PASSING_DOUBLE_YELLOW = 8;
  }

  // 车道线类型
  optional RoadLineType type = 1;

  // 车道边界采样点
  repeated MapPoint polyline = 2;
}
```
#### RoadEdge
道路边界，包括类型以及采样点。

```
message RoadEdge {
  // 道路边沿类型.
  enum RoadEdgeType {
    TYPE_UNKNOWN = 0;
    // 物理隔离，旁边不会走车
    TYPE_ROAD_EDGE_BOUNDARY = 1;
    // 物理隔离，旁边会走车（绿化带，隔离带）
    TYPE_ROAD_EDGE_MEDIAN = 2;
  }

  // 道路边沿类型.
  optional RoadEdgeType type = 1;

  // 道路边缘采样点
  repeated MapPoint polyline = 2;
}
```
#### StopSign
停止标志

```
message StopSign {
  // 停止标志关联的车道id
  repeated int64 lane = 1;

  // 位置
  optional MapPoint position = 2;
}
```
#### Crosswalk
人行横道

```
message Crosswalk {
  // 假定是闭合的（第一个点和最后一个点连接起来）
  repeated MapPoint polygon = 1;
}
```

#### SpeedBump
减速带
```
message SpeedBump {
  // 假定是闭合的（第一个点和最后一个点连接起来）
  repeated MapPoint polygon = 1;
}
```

#### 总结
waymo数据集中的地图格式和apollo中的地图格式类似，都采用了protobuf格式，而且都是对车道进行采样，用一系列的采样点来表示，类型也基本一致包括lane、road、红绿灯、减速带等。但是关于相邻车道部分的定义有一定的区别。


