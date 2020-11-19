# MBPO PyTorch
A PyTorch reimplementation of MBPO (When to trust your model: model-based policy optimization)

# Dependency

Please refer to ./requirements.txt.

# Usage

    python ./mbpo/scripts/run_mbpo.py
    
    # you can also overwrite hyperparams by passing args, e.g.
    python ./mbpo/scripts/run_mbpo.py --set seed=0 verbose=1 device="'cuda:0'"

  hyperparams in ./configs/mbpo.yaml

# Credits
1. [vitchyr/rlkit](https://github.com/vitchyr/rlkit)
2. [facebookresearch/slbo](https://github.com/JannerM/mbpo)