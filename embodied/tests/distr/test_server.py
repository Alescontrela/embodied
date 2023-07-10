import pathlib
import sys
import threading
import time

sys.path.append(str(pathlib.Path(__file__).parent.parent.parent.parent))

import embodied
import numpy as np
import pytest

SERVERS = [
    embodied.distr.Server,
    embodied.distr.Server2,
]

PORTS = iter(range(5555, 6000))

def addresses(tcp=True, ipc=True):
  results = []
  tcp and results.append('tcp://localhost:{port}')
  ipc and results.append('ipc:///tmp/test-{port}')
  return results


class TestServer:

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_single_client(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    def function(data):
      assert data == {'foo': np.array(1)}
      return {'foo': 2 * data['foo']}
    server = Server(addr)
    server.bind('function', function)
    with server:
      client = embodied.distr.Client(addr, pings=0, maxage=1)
      client.connect(retry=False, timeout=1)
      future = client.function({'foo': np.array(1)})
      result = future.result()
      assert result['foo'] == 2

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_multiple_clients(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    server = Server(addr)
    server.bind('function', lambda data: data)
    with server:
      clients = []
      for i in range(10):
        client = embodied.distr.Client(addr, i, pings=0, maxage=1)
        client.connect()
        clients.append(client)
      futures = [
          client.function({'foo': i}) for i, client in enumerate(clients)]
      results = [future.result()['foo'] for future in futures]
      assert results == list(range(10))

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_multiple_methods(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    server = Server(addr)
    server.bind('add', lambda data: {'z': data['x'] + data['y']})
    server.bind('sub', lambda data: {'z': data['x'] - data['y']})
    with server:
      client = embodied.distr.Client(addr, pings=0, maxage=0.1)
      client.connect(retry=False, timeout=1)
      assert client.add({'x': 42, 'y': 13}).result()['z'] == 55
      assert client.sub({'x': 42, 'y': 13}).result()['z'] == 29

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_future_order(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    server = Server(addr)
    server.bind('function', lambda data: data)
    with server:
      client = embodied.distr.Client(addr, 0, pings=0, maxage=1)
      client.connect(retry=False, timeout=1)
      future1 = client.function({'foo': 1})
      future2 = client.function({'foo': 2})
      future3 = client.function({'foo': 3})
      assert future2.result()['foo'] == 2
      assert future1.result()['foo'] == 1
      assert future3.result()['foo'] == 3

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_future_cleanup(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    server = Server(addr)
    server.bind('function', lambda data: data)
    with server:
      client = embodied.distr.Client(addr, 0, pings=0, maxage=1)
      client.connect(retry=False, timeout=1)
      client.function({'foo': 1})
      client.function({'foo': 2})
      future3 = client.function({'foo': np.array(3)})
      assert future3.result()['foo'] == 3
      del future3
      assert not list(client.futures.keys())

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_ping_alive(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    def slow(data):
      time.sleep(0.1)
      return data
    server = Server(addr)
    server.bind('function', slow)
    with server:
      client = embodied.distr.Client(addr, pings=0.01, maxage=0.05)
      client.connect()
      assert client.function({'foo': 0}).result() == {'foo': 0}

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_ping_dead(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    def slow(data):
      time.sleep(0.2)
      return data
    server = Server(addr)
    server.bind('function', slow)
    with server:
      client = embodied.distr.Client(addr, pings=0.1, maxage=0.01)
      client.connect()
      with pytest.raises(embodied.distr.NotAliveError):
        client.function({'foo': 0}).result()

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_remote_error(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    def error(data):
      raise RuntimeError('foo')
    server = Server(addr)
    server.bind('function', error)
    with server:
      client = embodied.distr.Client(addr)
      client.connect()
      future = client.function({'bar': 0})
      with pytest.raises(embodied.distr.RemoteError) as info1:
        future.result()
      with pytest.raises(RuntimeError) as info2:
        server.check()
    assert repr(info1.value) == '''RemoteError("RuntimeError('foo')")'''
    assert repr(info2.value) == "RuntimeError('foo')"

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_logfn_ordered(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    rng = np.random.default_rng(0)
    completed = []
    logged = []
    def sometimes_wait(data):
      if rng.uniform() < 0.5:
        time.sleep(0.1)
      completed.append(data['i'])
      return data, data
    def logfn(data):
      logged.append(data['i'])
    server = Server(addr, workers=2)
    server.bind('function', sometimes_wait, logfn)
    with server:
      client = embodied.distr.Client(addr, pings=0, maxage=1)
      client.connect()
      futures = [client.function({'i': i}) for i in range(10)]
      results = [future.result()['i'] for future in futures]
    assert results == list(range(10))
    assert logged == list(range(10))
    assert completed != list(range(10))

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_connect_retry(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    results = []
    def client():
      try:
        client = embodied.distr.Client(addr)
        client.connect(retry=True, timeout=0.01)
        future = client.function({'foo': np.array(1)})
        results.append(future.result())
      except Exception as e:
        results.append(e)
    threading.Thread(target=client).start()
    time.sleep(0.2)
    server = Server(addr)
    server.bind('function', lambda data: data)
    with server:
      while not results:
        time.sleep(0.001)
    assert results == [{'foo': 1}]

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_shared_pool(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    def slow_function(data):
      time.sleep(0.1)
      return data
    def fast_function(data):
      time.sleep(0.01)
      return data
    server = Server(addr, workers=1)
    server.bind('slow_function', slow_function)
    server.bind('fast_function', fast_function)
    with server:
      client = embodied.distr.Client(addr)
      client.connect()
      slow_future = client.slow_function({'foo': 0})
      fast_future = client.fast_function({'foo': 0})
      assert not slow_future.done()
      fast_future.result()
      assert slow_future.done()

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  def test_separate_pools(self, Server, addr):
    addr = addr.format(port=next(PORTS))
    def slow_function(data):
      time.sleep(0.1)
      return data
    def fast_function(data):
      time.sleep(0.01)
      return data
    server = Server(addr)
    server.bind('slow_function', slow_function, workers=1)
    server.bind('fast_function', fast_function, workers=1)
    with server:
      client = embodied.distr.Client(addr)
      client.connect()
      slow_future = client.slow_function({'foo': 0})
      fast_future = client.fast_function({'foo': 0})
      fast_future.result()
      assert not slow_future.done()

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  @pytest.mark.parametrize('batch', (1, 2, 4))
  def test_batching_single(self, Server, addr, batch):
    addr = addr.format(port=next(PORTS))
    calls = [0]
    def function(data):
      assert set(data.keys()) == {'foo'}
      assert data['foo'].shape == (batch, 1)
      calls[0] += 1
      return data
    server = Server(addr)
    server.bind('function', function, batch=batch)
    with server:
      client = embodied.distr.Client(addr, pings=0, maxage=1)
      client.connect(retry=False, timeout=1)
      futures = [client.function({'foo': [i]}) for i in range(batch)]
      results = [future.result()['foo'][0] for future in futures]
      assert calls[0] == 1
      assert results == list(range(batch))

  @pytest.mark.parametrize('Server', SERVERS)
  @pytest.mark.parametrize('addr', addresses())
  @pytest.mark.parametrize('batch', (1, 2, 4))
  def test_batching_multiple(self, Server, addr, batch):
    addr = addr.format(port=next(PORTS))
    def function(data):
      return data
    server = Server(addr)
    server.bind('function', function, batch=batch)
    with server:
      clients = []
      for _ in range(3):
        client = embodied.distr.Client(addr, pings=0, maxage=1)
        client.connect(retry=False, timeout=1)
        clients.append(client)
      futures = ([], [], [])
      refs = ([], [], [])
      for n in range(batch):
        for i, client in enumerate(clients):
          futures[i].append(client.function({'foo': [i * n]}))
          refs[i].append(i * n)
      assert refs[0] == [x.result()['foo'][0] for x in futures[0]]
      assert refs[1] == [x.result()['foo'][0] for x in futures[1]]
      assert refs[2] == [x.result()['foo'][0] for x in futures[2]]
