import re
import sys
import threading
import time
from collections import defaultdict, deque

import embodied
import numpy as np


def parallel(agent, logger, make_replay, make_env, num_envs, args):
  step = logger.step
  usage = embodied.Usage(**args.usage)
  workers = []
  barrier = threading.Barrier(2)
  if args.env_processes and num_envs > 1:
    for i in range(num_envs):
      worker = embodied.distr.Process(parallel_env, i, make_env, args)
      worker.start()
      workers.append(worker)
    usage.add_procs('envs', workers)
  else:
    for i in range(num_envs):
      workers.append(embodied.distr.Thread(parallel_env, i, make_env, args))
  workers.append(embodied.distr.Process(
      parallel_replay, make_replay, args))
  workers.append(embodied.distr.Thread(
      parallel_actor, step, agent, logger, barrier, args))
  workers.append(embodied.distr.Thread(
      parallel_learner, step, agent, logger, usage, barrier, args))
  embodied.distr.run(workers)


def parallel_actor(step, agent, logger, barrier, args):

  # _, initial = agent.policy(dummy_data(agent.obs_space, (args.actor_batch,)))
  initial = agent.init_policy(args.actor_batch)
  initial = embodied.treemap(lambda x: x[0], initial)
  allstates = defaultdict(lambda: initial)
  barrier.wait()  # Do not collect data before learner restored checkpoint.

  @embodied.timer.section('actor_workfn')
  def workfn(obs, addrs):
    with embodied.timer.section('get_states'):
      states = [allstates[a] for a in addrs]
      states = embodied.treemap(lambda *xs: list(xs), *states)
    act, states = agent.policy(obs, states)
    act['reset'] = obs['is_last'].copy()
    with embodied.timer.section('put_states'):
      for i, a in enumerate(addrs):
        allstates[a] = embodied.treemap(lambda x: x[i], states)
    step.increment(args.actor_batch)
    logs = (obs, act, addrs)
    return act, logs

  should_log = embodied.when.Clock(args.log_every)
  parallel = embodied.Agg()
  epstats = embodied.Agg()
  episodes = defaultdict(embodied.Agg)
  updated = defaultdict(lambda: None)
  dones = defaultdict(lambda: True)
  nonzeros = set()

  replay = embodied.distr.Client('ipc:///tmp/replay', name='Inserter')
  replay.connect()
  replay_promises = deque()

  keys = set(agent.obs_space.keys()) | set(agent.act_space.keys())
  log_keys_max = [k for k in keys if re.match(args.log_keys_max, k)]
  log_keys_sum = [k for k in keys if re.match(args.log_keys_sum, k)]
  log_keys_avg = [k for k in keys if re.match(args.log_keys_avg, k)]
  log_keys = list(set(log_keys_max) | set(log_keys_sum) | set(log_keys_avg))

  @embodied.timer.section('actor_donefn')
  def donefn(logs):
    now = time.time()
    obs, act, addrs = logs
    trans = {**obs, **act}
    [x.setflags(write=False) for x in trans.values()]

    parallel.add('ep_states', len(allstates), agg='avg')
    parallel.add('ep_starts', trans['is_first'].sum(), agg='sum')
    parallel.add('ep_ends', trans['is_last'].sum(), agg='sum')

    with embodied.timer.section('inserts'):
      while len(replay_promises) > args.actor_threads:
        replay_promises.popleft()()  # Blocks when rate limited.
      request = {'method': 'add', 'worker': addrs, **trans}
      replay_promises.append(replay(request))

    for i, addr in enumerate(addrs):
      tran = {k: v[i] for k, v in trans.items()}

      # with embodied.timer.section('inserts'):
      #   replay.add(tran, worker=addr)  # Blocks when rate limited.

      with embodied.timer.section('logs1'):
        updated[addr] = now
        episode = episodes[addr]
        if tran['is_first']:
          episode.reset()
          parallel.add('ep_abandoned', int(not dones[addr]), agg='sum')
        dones[addr] = tran['is_last']

      with embodied.timer.section('logs2'):
        episode.add('score', tran['reward'], agg='sum')
        episode.add('length', 1, agg='sum')
        episode.add('rewards', tran['reward'], agg='stack')

      with embodied.timer.section('logs3'):
        video_addrs = list(episodes.keys())[:args.log_video_streams]
        if addr in video_addrs:
          for key in args.log_keys_video:
            if key in tran:
              episode.add(f'policy_{key}', tran[key], agg='stack')

      # TODO: This is really expensive in terms of GIL usage.
      with embodied.timer.section('logs4'):
        if not args.log_zeros:
          for key in log_keys:
            if key not in nonzeros and np.any(tran[key] != 0):
              nonzeros.add(key)
        for key in log_keys_sum:
          if args.log_zeros or key in nonzeros:
            episode.add(key, tran[key], agg='sum')
        for key in log_keys_avg:
          if args.log_zeros or key in nonzeros:
            episode.add(key, tran[key], agg='avg')
        for key in log_keys_max:
          if args.log_zeros or key in nonzeros:
            episode.add(key, tran[key], agg='max')

      with embodied.timer.section('logs5'):
        if tran['is_last']:
          result = episode.result()
          logger.add({
              'score': result.pop('score'),
              'length': result.pop('length') - 1,
          }, prefix='episode')
          rew = result.pop('rewards')
          result['reward_rate'] = (rew - rew.min() >= 0.1).mean()
          epstats.add(result)

    with embodied.timer.section('drops'):
      for addr, last in list(updated.items()):
        if now - last >= args.log_episode_timeout:
          print('Dropping episode statistics due to timeout.')
          del episodes[addr]
          del updated[addr]

    if should_log():
      with embodied.timer.section('actor_metrics'):
        logger.add(parallel.result(), prefix='parallel')
        logger.add(epstats.result(), prefix='epstats')
        logger.add(server.stats(), prefix='server')

  addr = f'tcp://*:{args.actor_port}'
  with embodied.distr.BatchServer(
      addr, workfn, donefn, args.actor_batch, args.actor_threads,
      args.ipv6) as server:
    while True:
      server.check()
      time.sleep(1)


def parallel_learner(step, agent, logger, usage, barrier, args):

  logdir = embodied.Path(args.logdir)
  agg = embodied.Agg()
  should_log = embodied.when.Clock(args.log_every)
  should_save = embodied.when.Clock(args.save_every)

  checkpoint = embodied.Checkpoint(logdir / 'checkpoint.ckpt')
  checkpoint.step = step
  checkpoint.agent = agent
  # checkpoint.replay = replay
  if args.from_checkpoint:
    checkpoint.load(args.from_checkpoint)
  checkpoint.load_or_save()
  barrier.wait()
  should_save(step)  # Register that we just saved.

  replay = embodied.distr.Client('ipc:///tmp/replay', name='Sampler')
  replay.connect()
  def parallel_dataset(prefetch=1):
    request = {'method': 'sample'}  # , 'batch': args.batch_size}
    promises = deque([replay(request) for _ in range(prefetch)])
    while True:
      promises.append(replay(request))
      yield promises.popleft()()

  # dataset = agent.dataset(replay.dataset)
  dataset = agent.dataset(parallel_dataset)

  state = None
  # TODO
  # state = agent.init_train(len(next(dataset)['is_first']))
  stats = dict(last_time=time.time(), last_step=int(step), batch_entries=0)
  while True:

    with embodied.timer.section('learner_batch_next'):
      batch = next(dataset)
    with embodied.timer.section('learner_train_step'):
      outs, state, mets = agent.train(batch, state)
    agg.add(mets)
    stats['batch_entries'] += batch['is_first'].size

    if should_log():
      with embodied.timer.section('learner_metrics'):
        logger.add(agg.result(), prefix='train')
        logger.add(agent.report(batch), prefix='report')
        logger.add(embodied.timer.stats(), prefix='timer')
        # logger.add(replay.stats(), prefix='replay')
        logger.add(replay({'method': 'stats'})(), prefix='replay')
        logger.add(usage.stats(), prefix='usage')
        duration = time.time() - stats['last_time']
        actor_fps = (int(step) - stats['last_step']) / duration
        learner_fps = stats['batch_entries'] / duration
        train_ratio = learner_fps / actor_fps if actor_fps else np.inf
        logger.add({
            'actor_fps': actor_fps,
            'learner_fps': learner_fps,
            'train_ratio': train_ratio,
        }, prefix='parallel')
        stats = dict(
            last_time=time.time(), last_step=int(step), batch_entries=0)
      logger.write(fps=True)

    if should_save():
      checkpoint.save()


def parallel_replay(make_replay, args):
  replay = make_replay()
  dataset = iter(replay.dataset())
  def workfn(data, _):
    method = data.pop('method')
    print('REPLAY METHOD', method)
    if method == 'add':
      for i, worker in enumerate(data.pop('worker')):
        replay.add({k: v[i] for k, v in data.items()}, worker)
      return {}
    # TODO
    # elif method == 'sample':
    #   batch = data['batch']
    #   [next(dataset) for _ in range(data['batch'])]
    elif method == 'sample':
      return next(dataset)
    elif method == 'stats':
      return replay.stats()
    else:
      raise NotImplementedError(f'Unknown method {method}')
  # TODO: How to make sure we serve inserts when samples are blocked and vice
  # versa? Need two server instances?
  with embodied.distr.Server('ipc:///tmp/replay', workfn, workers=64) as server:
    while True:
      server.check()
      time.sleep(1)


def parallel_env(replica_id, make_env, args):
  assert replica_id >= 0, replica_id
  rid = replica_id
  name = f'Env{rid}'
  _print = lambda x: embodied.print(f'[{name}] {x}')

  step = embodied.Counter()
  logger = embodied.Logger(step, [embodied.logger.TerminalOutput(name=name)])
  usage = embodied.Usage(psutil=True)
  should_log = embodied.when.Clock(args.log_every)

  _print('Make env')
  env = make_env()
  # addr = f'{args.actor_host}:{args.actor_port}'
  addr = f'tcp://{args.actor_host}:{args.actor_port}'
  actor = embodied.distr.Client(addr, rid, name, args.ipv6)

  done = True
  while True:
    if done:
      act = {k: v.sample() for k, v in env.act_space.items()}
      act['reset'] = True
      score, length = 0, 0
    with embodied.timer.section('env_step'):
      obs = env.step(act)
    obs = {k: np.asarray(v) for k, v in obs.items()}
    score += obs['reward']
    length += 1
    done = obs['is_last']
    if done:
      _print(f'Episode of length {length} with score {score:.4f}')
      logger.scalar('score', score)
      logger.scalar('length', length)
    with embodied.timer.section('env_request'):
      promise = actor(obs)
    try:
      with embodied.timer.section('env_response'):
        act = promise()
      act = {k: v for k, v in act.items() if not k.startswith('log_')}
    except embodied.distr.NotAliveError:
      # Wait until we are connected again, so we don't unnecessarily reset the
      # environment hundreds of times while the server is unavailable.
      _print('Lost connection to server')
      actor.connect()
      _print('Starting new episode')
      done = True
    except embodied.distr.RemoteError as e:
      _print(f'Shutting down env due to agent error: {e}')
      sys.exit(0)

    # step.increment()
    # if should_log():
    #   logger.add(usage.stats(), prefix='usage')
    #   logger.add(embodied.timer.stats(), prefix='timer')
    #   logger.write(fps=True)


def dummy_data(spaces, batch_dims):
  # TODO: Get rid of this function by adding initial_policy_state() and
  # initial_train_state() to the agent API. Or move this helper into
  # core/basics?
  spaces = list(spaces.items())
  data = {k: np.zeros(v.shape, v.dtype) for k, v in spaces}
  for dim in reversed(batch_dims):
    data = {k: np.repeat(v[None], dim, axis=0) for k, v in data.items()}
  return data
