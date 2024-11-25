from metadrive.envs.gym_wrapper import createGymWrapper
from metadrive.envs.metadrive_env import MetaDriveEnv
import matplotlib.pyplot as plt

env_config = {"accident_prob": 1.0}
gym_env = createGymWrapper(MetaDriveEnv)(env_config)
try:
    o = gym_env.reset()
    o, r, d, i = gym_env.step([0, 0])
    assert gym_env.config["accident_prob"] == 1.0
    print("Vehicle id:", gym_env.agent.id)
    ret = gym_env.render(mode="topdown",
                         window=False,
                         camera_position=(50, -70))
finally:
    gym_env.close()

plt.axis('off')
plt.imshow(ret)