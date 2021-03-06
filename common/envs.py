import os
import threading

import gym
import numpy as np

from pysc2.env import sc2_env, available_actions_printer
from pysc2.lib import actions
from pysc2.lib.actions import get_arg_size_lookup
from pysc2.lib.buffs import get_buff_embed_lookup
from pysc2.lib.features import ScreenFeatures, Player, FeatureUnit
from pysc2.lib.units import get_unit_embed_lookup


class DMC:

    def __init__(self, name, action_repeat=1, size=(64, 64), camera=None):
        os.environ['MUJOCO_GL'] = 'egl'
        domain, task = name.split('_', 1)
        if domain == 'cup':  # Only domain with multiple words.
            domain = 'ball_in_cup'
        if isinstance(domain, str):
            from dm_control import suite
            self._env = suite.load(domain, task)
        else:
            assert task is None
            self._env = domain()
        self._action_repeat = action_repeat
        self._size = size
        if camera is None:
            camera = dict(quadruped=2).get(domain, 0)
        self._camera = camera

    @property
    def observation_space(self):
        spaces = {}
        for key, value in self._env.observation_spec().items():
            spaces[key] = gym.spaces.Box(
                -np.inf, np.inf, value.shape, dtype=np.float32)
        spaces['image'] = gym.spaces.Box(
            0, 255, self._size + (3,), dtype=np.uint8)
        return gym.spaces.Dict(spaces)

    @property
    def action_space(self):
        spec = self._env.action_spec()
        action = gym.spaces.Box(spec.minimum, spec.maximum, dtype=np.float32)
        return gym.spaces.Dict({'action': action})

    def step(self, action):
        action = action['action']
        assert np.isfinite(action).all(), action
        reward = 0
        for _ in range(self._action_repeat):
            time_step = self._env.step(action)
            reward += time_step.reward or 0
            if time_step.last():
                break
        obs = dict(time_step.observation)
        obs['image'] = self.render()
        done = time_step.last()
        info = {'discount': np.array(time_step.discount, np.float32)}
        return obs, reward, done, info

    def reset(self):
        time_step = self._env.reset()
        obs = dict(time_step.observation)
        obs['image'] = self.render()
        return obs

    def render(self, *args, **kwargs):
        if kwargs.get('mode', 'rgb_array') != 'rgb_array':
            raise ValueError("Only render mode 'rgb_array' is supported.")
        return self._env.physics.render(*self._size, camera_id=self._camera)


class Atari:
    LOCK = threading.Lock()

    def __init__(
        self, name, action_repeat=4, size=(84, 84), grayscale=True, noops=30,
        life_done=False, sticky_actions=True, all_actions=False):
        assert size[0] == size[1]
        import gym.wrappers
        import gym.envs.atari
        if name == 'james_bond':
            name = 'jamesbond'
        with self.LOCK:
            env = gym.envs.atari.AtariEnv(
                game=name, obs_type='image', frameskip=1,
                repeat_action_probability=0.25 if sticky_actions else 0.0,
                full_action_space=all_actions)
        # Avoid unnecessary rendering in inner env.
        env._get_obs = lambda: None
        # Tell wrapper that the inner env has no action repeat.
        env.spec = gym.envs.registration.EnvSpec('NoFrameskip-v0')
        mean = env.unwrapped.get_action_meanings()

        env = gym.wrappers.AtariPreprocessing(
            env, noops, action_repeat, size[0], life_done, grayscale)
        self._env = env
        self._grayscale = grayscale

    @property
    def observation_space(self):
        return gym.spaces.Dict({
            'image': self._env.observation_space,
            'ram': gym.spaces.Box(0, 255, (128,), np.uint8),
        })

    @property
    def action_space(self):
        return {'action': self._env.action_space}

    def close(self):
        return self._env.close()

    def reset(self):
        with self.LOCK:
            image = self._env.reset()
        if self._grayscale:
            image = image[..., None]
        obs = {'image': image, 'ram': self._env.env._get_ram()}
        return obs

    def step(self, action):
        action = action['action']
        image, reward, done, info = self._env.step(action)
        if self._grayscale:
            image = image[..., None]
        obs = {'image': image, 'ram': self._env.env._get_ram()}
        return obs, reward, done, info

    def render(self, mode):
        return self._env.render(mode)


class Sc2:
    def __init__(self, map_name, screen_size, minimap_size, max_units, steps_per_action, steps_per_episode, fog, visualise):

        self.blocked_actions = [
            4   # control groups
        ]

        self.unit_embed_lookup = get_unit_embed_lookup()
        self.buff_embed_lookup = get_buff_embed_lookup()
        self.action_embed_lookup = self._get_action_lookup()
        self.action_id_lookup = dict((reversed(item) for item in self.action_embed_lookup.items()))
        self.args_size_lookup = get_arg_size_lookup(screen_size, minimap_size)
        self.unit_max = max_units

        from absl import flags
        flags.FLAGS.mark_as_parsed()
        env = sc2_env.SC2Env(
            map_name=map_name,
            battle_net_map=False,
            players=[sc2_env.Agent(sc2_env.Race.random, 'agent')],
            agent_interface_format=sc2_env.parse_agent_interface_format(
                feature_screen=screen_size,
                feature_minimap=minimap_size,
                use_feature_units=True,  # units in screen view
                use_raw_units=False  # all units including outside screen / invis
            ),
            step_mul=steps_per_action,
            game_steps_per_episode=steps_per_episode,
            disable_fog=fog,
            visualize=visualise)
        env = available_actions_printer.AvailableActionsPrinter(env)
        self._env = env


    @property
    def available_actions(self):
        return self._env.action_spec()[0]

    @property
    def observation_space(self):
        image = gym.spaces.Box(0, 255, (64, 64, 3), dtype=np.uint8)
        return gym.spaces.Dict({'image': image})

    def _get_arg_keys(self, arg_id):
        keys = []
        for i, size in enumerate(self.args_size_lookup[arg_id]):
            keys.append(f'arg_{arg_id}_{i}')
        return keys

    def _get_action_lookup(self):
        lookup = {}
        index = 0

        for f in actions._FUNCTIONS:
            if f.general_id == 0 and f.id not in self.blocked_actions:
                lookup[int(f.id)] = index
                index += 1

        return lookup

    @property
    def action_space(self):
        action_id = gym.spaces.Discrete(len(self.action_embed_lookup))
        action_args = {'action_id': action_id}

        # the arg lookup is a dict of ranges of each arg, create a one-hot for each
        for arg_id in self.args_size_lookup:
            arg_keys = self._get_arg_keys(arg_id)
            for i, size in enumerate(self.args_size_lookup[arg_id]):
                action_args[arg_keys[i]] = gym.spaces.Discrete(size)

        return action_args

    # defines which actions require which args, used for training to prevent backprop through un-used arguments
    # also used to input the correct args into the sc environment
    @property
    def action_arg_lookup(self):
        action_spec = self._env.action_spec()[0]

        action_arg_reqs = {}
        for action_id in self.action_id_lookup:
            args_list = []
            required_args = action_spec.functions[self.action_id_lookup[action_id]].args
            for r in required_args:
                args_list += self._get_arg_keys(r.id)
            action_arg_reqs[action_id] = args_list

        return action_arg_reqs

    def step(self, action):
        args = []

        # rebuild the action into sc2 ids and arguments
        action_id = self.action_id_lookup[action['action_id']]

        required_args = [arg for arg in self.available_actions.functions[action_id].args]

        for r in required_args:
            arg_set = []
            for k in self._get_arg_keys(r.id):
                arg_set.append(action[k])
            args.append(arg_set)

        sc2_action = actions.FunctionCall(action_id, args)

        timestep = self._env.step([sc2_action])[0]
        obs = self.collect_sc_observation(timestep)
        reward = float(timestep.reward)

        done = False
        if timestep.last():
            done = True

        info = {}
        return obs, reward, done, info

    def reset(self):
        timestep = self._env.reset()[0]
        obs = self.collect_sc_observation(timestep)

        return obs

    def collect_sc_observation(self, timestep):
        obs = {}

        av_actions = timestep.observation.available_actions
        action_indices = [self.action_embed_lookup[a] for a in av_actions if a not in self.blocked_actions]
        action_categorical = np.zeros(len(self.action_embed_lookup), dtype=np.int)
        action_categorical[action_indices] = 1
        obs['available_actions'] = action_categorical

        # screen features
        screen_feat = timestep.observation.feature_screen
        obs['screen'] = np.stack([screen_feat.visibility_map,
                                  screen_feat.height_map,
                                  screen_feat.creep,
                                  screen_feat.buildable,
                                  screen_feat.pathable,
                                  screen_feat.effects],
                                 axis=2)

        # minimap features
        mini_feat = timestep.observation.feature_minimap
        obs['mini'] = np.stack([mini_feat.visibility_map,
                                mini_feat.height_map,
                                mini_feat.player_relative,
                                mini_feat.creep,
                                mini_feat.buildable,
                                mini_feat.pathable,
                                mini_feat.camera],
                               axis=2)

        # player features
        player_feat = timestep.observation.player
        obs['player'] = player_feat[[Player.minerals,
                                     Player.vespene,
                                     Player.food_used,
                                     Player.food_cap,
                                     Player.larva_count,
                                     Player.warp_gate_count]]

        # units on the screen => limit to 200
        units = timestep.observation.feature_units
        unit_type_ids = list(units[:, 0])

        unit_embed_ids = np.expand_dims(np.array([self.unit_embed_lookup[int(x)] for x in unit_type_ids], dtype=np.long), 1)
        unit_features = units[:, [FeatureUnit.alliance,
                                  FeatureUnit.health_ratio,
                                  FeatureUnit.shield_ratio,
                                  FeatureUnit.energy_ratio,
                                  FeatureUnit.x,
                                  FeatureUnit.y,
                                  FeatureUnit.radius,
                                  FeatureUnit.is_selected,
                                  FeatureUnit.is_blip,
                                  FeatureUnit.build_progress,   # feature indexes will be +1 once merged with id array
                                  FeatureUnit.is_powered,
                                  FeatureUnit.mineral_contents,
                                  FeatureUnit.vespene_contents,
                                  FeatureUnit.cargo_space_taken,
                                  FeatureUnit.cargo_space_max,
                                  FeatureUnit.is_flying,
                                  FeatureUnit.is_burrowed,
                                  FeatureUnit.is_in_cargo,
                                  FeatureUnit.cloak,
                                  FeatureUnit.hallucination,
                                  FeatureUnit.attack_upgrade_level,
                                  FeatureUnit.armor_upgrade_level,
                                  FeatureUnit.shield_upgrade_level,

                                  # buff features
                                  FeatureUnit.Vespene_carry,
                                  FeatureUnit.Blinding_cloud,
                                  FeatureUnit.Hold_fire,
                                  FeatureUnit.Cloak_ability,
                                  FeatureUnit.Stim,
                                  FeatureUnit.AmorphousArmorcloud,
                                  FeatureUnit.BatteryOvercharge,
                                  FeatureUnit.CarryHighYieldMineralFieldMinerals,
                                  FeatureUnit.CarryMineralFieldMinerals,
                                  FeatureUnit.ChannelSnipeCombat,
                                  FeatureUnit.Charging,
                                  FeatureUnit.ChronoBoostEnergyCost,
                                  FeatureUnit.CloakFieldEffect,
                                  FeatureUnit.Contaminated,
                                  FeatureUnit.EMPDecloak,
                                  FeatureUnit.FungalGrowth,
                                  FeatureUnit.GravitonBeam,
                                  FeatureUnit.GuardianShield,
                                  FeatureUnit.ImmortalOverload,
                                  FeatureUnit.InhibitorZoneTemporalField,
                                  FeatureUnit.LockOn,
                                  FeatureUnit.MedivacSpeedBoost,
                                  FeatureUnit.NeuralParasite,
                                  FeatureUnit.OracleRevelation,
                                  FeatureUnit.OracleStasisTrapTarget,
                                  FeatureUnit.OracleWeapon,
                                  FeatureUnit.ParasiticBomb,
                                  FeatureUnit.ParasiticBombSecondaryUnitSearch,
                                  FeatureUnit.ParasiticBombUnitKU,
                                  FeatureUnit.PowerUserWarpable,
                                  FeatureUnit.PsiStorm,
                                  FeatureUnit.QueenSpawnLarvaTimer,
                                  FeatureUnit.RavenScramblerMissile,
                                  FeatureUnit.RavenShredderMissileArmorReduction,
                                  FeatureUnit.RavenShredderMissileTint,
                                  FeatureUnit.Slow,
                                  FeatureUnit.SupplyDrop,
                                  FeatureUnit.TemporalField,
                                  FeatureUnit.ViperConsumeStructure,
                                  FeatureUnit.VoidRaySpeedUpgrade,
                                  FeatureUnit.VoidRaySwarmDamageBoost
                                  ]]

        units_out = np.concatenate([unit_embed_ids, unit_features], 1)

        unit_dim = self.unit_max - np.size(units_out, 0)
        feature_dim = np.size(units_out, 1)
        unit_padding = np.zeros((unit_dim, feature_dim), dtype=np.long)
        obs['units'] = np.concatenate([units_out, unit_padding])
        obs['unit_count'] = np.size(units_out, 0)

        return obs

    def close(self):
        return self._env.close()


class Dummy:

    def __init__(self):
        pass

    @property
    def observation_space(self):
        image = gym.spaces.Box(0, 255, (64, 64, 3), dtype=np.uint8)
        return gym.spaces.Dict({'image': image})

    @property
    def action_space(self):
        action = gym.spaces.Box(-1, 1, (6,), dtype=np.float32)
        return gym.spaces.Dict({'action': action})

    def step(self, action):
        obs = {'image': np.zeros((64, 64, 3))}
        reward = 0.0
        done = False
        info = {}
        return obs, reward, done, info

    def reset(self):
        obs = {'image': np.zeros((64, 64, 3))}
        return obs


class TimeLimit:

    def __init__(self, env, duration):
        self._env = env
        self._duration = duration
        self._step = None

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        assert self._step is not None, 'Must reset environment.'
        obs, reward, done, info = self._env.step(action)
        self._step += 1
        if self._step >= self._duration:
            done = True
            if 'discount' not in info:
                info['discount'] = np.array(1.0).astype(np.float32)
            self._step = None
        return obs, reward, done, info

    def reset(self):
        self._step = 0
        return self._env.reset()


class NormalizeAction:

    def __init__(self, env, key='action'):
        self._env = env
        self._key = key
        space = env.action_space[key]
        self._mask = np.isfinite(space.low) & np.isfinite(space.high)
        self._low = np.where(self._mask, space.low, -1)
        self._high = np.where(self._mask, space.high, 1)

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def action_space(self):
        low = np.where(self._mask, -np.ones_like(self._low), self._low)
        high = np.where(self._mask, np.ones_like(self._low), self._high)
        space = gym.spaces.Box(low, high, dtype=np.float32)
        return {**self._env.action_space.spaces, self._key: space}

    def step(self, action):
        orig = (action[self._key] + 1) / 2 * (self._high - self._low) + self._low
        orig = np.where(self._mask, orig, action[self._key])
        return self._env.step({**action, self._key: orig})


class OneHotAction:

    def __init__(self, env):
        self._env = env
        self._random = np.random.RandomState()
        self._keys = self._env.action_space.keys()

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def action_space(self):
        og_space = self._env.action_space
        new_space = {}
        for k in self._keys:
            shape = (og_space[k].n,)
            space = gym.spaces.Box(low=0, high=1, shape=shape, dtype=np.float32)
            space.sample = self._sample_action
            space.n = shape[0]
            new_space[k] = space
        return new_space

    def step(self, action):
        action_indices = {}

        for k in self._keys:
            index = np.argmax(action[k]).astype(int)
            reference = np.zeros_like(action[k])
            reference[index] = 1
            if not np.allclose(reference, action[k]):
                action_indices[k] = -1
            else:
                action_indices[k] = index
        return self._env.step(action_indices)

    def reset(self):
        return self._env.reset()

    def _sample_action(self):
        actions = self._env.action_space.n
        index = self._random.randint(0, actions)
        reference = np.zeros(actions, dtype=np.float32)
        reference[index] = 1.0
        return reference


class RewardObs:

    def __init__(self, env, key='reward'):
        assert key not in env.observation_space.spaces
        self._env = env
        self._key = key

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def observation_space(self):
        space = gym.spaces.Box(-np.inf, np.inf, (), dtype=np.float32)
        return gym.spaces.Dict({
            **self._env.observation_space.spaces, self._key: space})

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs['reward'] = reward
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        obs['reward'] = 0.0
        return obs


class ResetObs:

    def __init__(self, env, key='reset'):
        assert key not in env.observation_space.spaces
        self._env = env
        self._key = key

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def observation_space(self):
        space = gym.spaces.Box(0, 1, (), dtype=np.bool)
        return gym.spaces.Dict({
            **self._env.observation_space.spaces, self._key: space})

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs['reset'] = np.array(False, np.bool)
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        obs['reset'] = np.array(True, np.bool)
        return obs
