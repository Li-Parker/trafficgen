import torch
print(torch.cuda.is_available())
print(torch.__version__)
from metadrive import MetaDriveEnv
env = MetaDriveEnv()
obs = env.reset()
print(obs)  # 输出 (259,)

