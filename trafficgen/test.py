import pickle

path1 = "D:\Desktop\TrustedArtificialIntelligence\实习\\trafficgen\\trafficgen\data\lanfeng\waymo\init_data\\0.pkl"
path2 = "D:\Desktop\TrustedArtificialIntelligence\实习\\trafficgen\\trafficgen\data\lanfeng\waymo_data\init_data\init_cache.pkl"
with open(path1, 'rb') as f:
    a = pickle.load(f)

with open(path2, 'rb') as f:
    b = pickle.load(f)
print("end....")
