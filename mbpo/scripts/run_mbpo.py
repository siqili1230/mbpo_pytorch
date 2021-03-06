import time
from collections import deque

import numpy as np
import torch
from operator import itemgetter

from mbpo.algos import MBPO, SAC
from mbpo.configs.config import Config
from mbpo.envs.wrapped_envs import make_vec_envs, make_vec_virtual_envs
from mbpo.misc import logger
from mbpo.misc.utils import set_seed, get_seed, log_and_write, evaluate, commit_and_save, init_logging
from mbpo.models import Actor, QCritic, RunningNormalizer, EnsembleRDynamics
from mbpo.storages import SimpleUniversalOffPolicyBuffer as Buffer, MixtureBuffer


# noinspection DuplicatedCode
def main():
    config, hparam_dict = Config('mbpo.yaml')
    set_seed(config.seed)
    commit_and_save(config.proj_dir, config.save_dir, False, False)

    writer, log_dir, eval_log_dir, save_dir = init_logging(config, hparam_dict)

    mf_config = config.sac
    mb_config = config.mbpo

    device = torch.device(config.device)

    real_envs = make_vec_envs(config.env.env_name, get_seed(), config.env.num_real_envs,  config.env.gamma,
                              log_dir, device, allow_early_resets=True, norm_reward=False, norm_obs=False)

    state_dim = real_envs.observation_space.shape[0]
    action_space = real_envs.action_space
    action_dim = action_space.shape[0]

    datatype = {'states': {'dims': [state_dim]}, 'next_states': {'dims': [state_dim]},
                'actions': {'dims': [action_dim]}, 'rewards': {'dims': [1]}, 'masks': {'dims': [1]}}

    state_normalizer = RunningNormalizer(state_dim)
    action_normalizer = RunningNormalizer(action_dim)
    state_normalizer.to(device)
    action_normalizer.to(device)

    dynamics = EnsembleRDynamics(state_dim, action_dim, 1, config.mbpo.dynamics_hidden_dims,
                                 mb_config.num_dynamics_networks, mb_config.num_elite_dynamics_networks,
                                 state_normalizer, action_normalizer)
    dynamics.to(device)

    actor = Actor(state_dim, action_space, mf_config.actor_hidden_dims, state_normalizer=None,
                  use_limited_entropy=False, use_tanh_squash=True, use_state_dependent_std=True)
    actor.to(device)

    q_critic1 = QCritic(state_dim, action_space, mf_config.critic_hidden_dims)
    q_critic2 = QCritic(state_dim, action_space, mf_config.critic_hidden_dims)
    q_critic_target1 = QCritic(state_dim, action_space, mf_config.critic_hidden_dims)
    q_critic_target2 = QCritic(state_dim, action_space, mf_config.critic_hidden_dims)
    q_critic1.to(device)
    q_critic2.to(device)
    q_critic_target1.to(device)
    q_critic_target2.to(device)

    target_entropy = mf_config.target_entropy or -np.prod(real_envs.action_space.shape).item()

    agent = SAC(actor, q_critic1, q_critic2, q_critic_target1, q_critic_target2, mf_config.batch_size,
                mf_config.num_grad_steps, config.env.gamma, 1.0, mf_config.actor_lr, mf_config.critic_lr,
                mf_config.soft_target_tau, target_entropy=target_entropy)

    virt_envs = make_vec_virtual_envs(config.env.env_name, dynamics, get_seed(), 0, config.env.gamma, device,
                                      use_predicted_reward=True)

    base_virtual_buffer_size = mb_config.rollout_batch_size * config.env.max_episode_steps * \
                               mb_config.num_model_retain_epochs // mb_config.model_update_interval

    virtual_buffer = Buffer(base_virtual_buffer_size, datatype)
    virtual_buffer.to(device)

    model = MBPO(dynamics, mb_config.dynamics_batch_size, rollout_schedule=mb_config.rollout_schedule, verbose=1,
                 lr=mb_config.lr, l2_loss_coefs=config.mbpo.l2_loss_coefs, max_num_epochs=mb_config.max_num_epochs)

    real_buffer = Buffer(mb_config.real_buffer_size, datatype)
    real_buffer.to(device)

    if mb_config.real_sample_ratio > 0:
        policy_buffer = MixtureBuffer([virtual_buffer, real_buffer],
                                      [(1 - mb_config.real_sample_ratio), mb_config.real_sample_ratio])
    else:
        policy_buffer = virtual_buffer

    if config.model_load_path is not None:
        raise NotImplemented
    if config.buffer_load_path is not None:
        raise NotImplemented

    real_states = real_envs.reset()

    real_episode_rewards = deque(maxlen=30)
    real_episode_lengths = deque(maxlen=30)

    for _ in range(mb_config.num_warmup_samples):
        real_actions = torch.tensor([real_envs.action_space.sample() for _ in range(config.env.num_real_envs)]).to(device)
        real_next_states, real_rewards, real_dones, real_infos = real_envs.step(real_actions)
        real_masks = torch.tensor([[0.0] if done else [1.0] for done in real_dones], dtype=torch.float32)
        real_buffer.insert(states=real_states, actions=real_actions, rewards=real_rewards, masks=real_masks,
                           next_states=real_next_states)
        real_states = real_next_states

        real_episode_rewards.extend([info['episode']['r'] for info in real_infos if 'episode' in info])
        real_episode_lengths.extend([info['episode']['l'] for info in real_infos if 'episode' in info])

    recent_states, recent_actions = itemgetter('states', 'actions')\
        (real_buffer.get_recent_samples(mb_config.num_warmup_samples - mb_config.model_update_interval))

    state_normalizer.update(recent_states)
    action_normalizer.update(recent_actions)

    start = time.time()

    for epoch in range(config.mbpo.num_total_epochs):
        logger.info('Epoch {}:'.format(epoch))

        model.update_rollout_length(epoch)

        for i in range(config.env.max_episode_steps):
            losses = {}
            if i % mb_config.model_update_interval == 0:
                recent_states, recent_actions = itemgetter('states', 'actions') \
                    (real_buffer.get_recent_samples(mb_config.model_update_interval))
                state_normalizer.update(recent_states)
                action_normalizer.update(recent_actions)

                losses.update(model.update(real_buffer))
                initial_states = next(real_buffer.get_batch_generator_inf(mb_config.rollout_batch_size))['states']
                new_virtual_buffer_size = base_virtual_buffer_size * model.num_rollout_steps
                virtual_buffer.resize(new_virtual_buffer_size)
                model.collect_data(virt_envs, virtual_buffer, initial_states, actor)

            with torch.no_grad():
                real_actions = actor.act(real_states)['actions']
            real_next_states, real_rewards, real_dones, real_infos = real_envs.step(real_actions)
            real_masks = torch.tensor([[0.0] if done else [1.0] for done in real_dones], dtype=torch.float32)
            real_buffer.insert(states=real_states, actions=real_actions, rewards=real_rewards, masks=real_masks,
                               next_states=real_next_states)
            state_normalizer.update(real_states)
            action_normalizer.update(real_actions)
            real_states = real_next_states
            real_episode_rewards.extend([info['episode']['r'] for info in real_infos if 'episode' in info])
            real_episode_lengths.extend([info['episode']['l'] for info in real_infos if 'episode' in info])

            losses.update(agent.update(policy_buffer))

            if i % 50 == 0:
                time_elapsed = time.time() - start
                num_env_steps = epoch * config.env.max_episode_steps + i
                if len(real_episode_rewards) > 0:
                    log_infos = [('perf/ep_rew_real', np.mean(real_episode_rewards)),
                                 ('perf/ep_len_real', np.mean(real_episode_lengths))]
                else:
                    log_infos = []
                for loss_name, loss_value in losses.items():
                    log_infos.append(('loss/' + loss_name, loss_value))
                log_infos.extend([('time_elapsed', time_elapsed), ('/fps', num_env_steps / time_elapsed )])
                log_and_write(logger, writer, log_infos, global_step=num_env_steps)

        if (epoch + 1) % config.eval_freq == 0:
            episode_rewards_real_eval, episode_lengths_real_eval = \
                evaluate(actor, config.env.env_name, get_seed(), 10, None, device,  norm_reward=False, norm_obs=False)
            log_infos = [('perf/ep_rew_real_eval', np.mean(episode_rewards_real_eval)),
                        ('perf/ep_len_real_eval', np.mean(episode_lengths_real_eval))]
            log_and_write(logger, writer, log_infos, global_step=(epoch + 1) * config.env.max_episode_steps)


if __name__ == '__main__':
    main()
