from copy import deepcopy
from contextlib import contextmanager
from multiprocessing import Process
from operator import add, mul
import socket
from time import time, sleep
from threading import Thread

import dask
from dask.core import get_deps
from toolz import merge
import pytest

from distributed3 import Center, Worker
from distributed3.utils import ignoring
from distributed3.client import gather_from_center
from distributed3.core import connect_sync, read_sync, write_sync
from distributed3.dask import _get, _get2, rewind, validate_state, heal

from tornado import gen
from tornado.ioloop import IOLoop


def inc(x):
    return x + 1


def _test_cluster(f, gets=[_get, _get2]):
    @gen.coroutine
    def g(get):
        c = Center('127.0.0.1', 8017)
        c.listen(c.port)
        a = Worker('127.0.0.1', 8018, c.ip, c.port, ncores=1)
        yield a._start()
        b = Worker('127.0.0.1', 8019, c.ip, c.port, ncores=1)
        yield b._start()

        while len(c.ncores) < 2:
            yield gen.sleep(0.01)

        try:
            yield f(c, a, b, get)
        finally:
            with ignoring():
                yield a._close()
            with ignoring():
                yield b._close()
            c.stop()

    for get in gets:
        IOLoop.current().run_sync(lambda: g(get))


def test_scheduler():
    dsk = {'x': 1, 'y': (add, 'x', 10), 'z': (add, (inc, 'y'), 20),
           'a': 1, 'b': (mul, 'a', 10), 'c': (mul, 'b', 20),
           'total': (add, 'c', 'z')}
    keys = ['total', 'c', ['z']]

    @gen.coroutine
    def f(c, a, b, get):
        result = yield get(c.ip, c.port, dsk, keys)
        result2 = yield gather_from_center((c.ip, c.port), result)

        expected = dask.async.get_sync(dsk, keys)
        assert tuple(result2) == expected
        assert set(a.data) | set(b.data) == {'total', 'c', 'z'}

    _test_cluster(f)


def test_scheduler_errors():
    def mydiv(x, y):
        return x / y
    dsk = {'x': 1, 'y': (mydiv, 'x', 0)}
    keys = 'y'

    @gen.coroutine
    def f(c, a, b, get):
        try:
            result = yield get(c.ip, c.port, dsk, keys)
            assert False
        except ZeroDivisionError as e:
            # assert 'mydiv' in str(e)
            pass

    _test_cluster(f)


def test_gather():
    dsk = {'x': 1, 'y': (inc, 'x')}
    keys = 'y'

    @gen.coroutine
    def f(c, a, b, get):
        result = yield get(c.ip, c.port, dsk, keys, gather=True)
        assert result == 2

    _test_cluster(f)


def test_heal():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    dependents = {'x': {'y'}, 'y': set()}

    in_memory = set()
    stacks = {'alice': [], 'bob': []}
    processing = {'alice': set(), 'bob': set()}

    waiting = {'x': set(), 'y': {'x'}}
    waiting_data = {'x': {'y'}}
    finished_results = set()
    released = set()

    local = {k: v for k, v in locals().items() if '@' not in k}

    output = heal(dsk, dependencies, dependents,
                  in_memory, stacks, processing, released)

    assert output['dsk'] == dsk
    assert output['dependencies'] == dependencies
    assert output['dependents'] == dependents
    assert output['in_memory'] == in_memory
    assert output['processing'] == processing
    assert output['stacks'] == stacks
    assert output['waiting'] == waiting
    assert output['waiting_data'] == waiting_data
    assert output['released'] == released

    state = {'in_memory': set(),
             'stacks': {'alice': ['x'], 'bob': []},
             'processing': {'alice': set(), 'bob': set()},
             'released': set()}

    heal(dsk, dependencies, dependents, **state)


def test_heal_2():
    dsk = {'x': 1, 'y': (inc, 'x'), 'z': (inc, 'y'),
           'a': 1, 'b': (inc, 'a'), 'c': (inc, 'b'),
           'result': (add, 'z', 'c')}
    dependencies = {'x': set(), 'y': {'x'}, 'z': {'y'},
                    'a': set(), 'b': {'a'}, 'c': {'b'},
                    'result': {'z', 'c'}}
    dependents = {'x': {'y'}, 'y': {'z'}, 'z': {'total'},
                  'a': {'b'}, 'b': {'z'}, 'c': {'total'},
                  'result': set()}

    state = {'in_memory': {'y', 'a'},  # missing 'b'
             'stacks': {'alice': ['z'], 'bob': []},
             'processing': {'alice': set(), 'bob': set(['c'])},
             'released': set()}

    output = heal(dsk, dependencies, dependents, **state)
    assert output['waiting'] == {'b': set(), 'c': {'b'}, 'result': {'c', 'z'}}
    assert output['waiting_data'] == {'a': {'b'}, 'b': {'c'}, 'c': {'result'},
                                      'y': {'z'}, 'z': {'result'}}
    assert output['in_memory'] == set(['y', 'a'])
    assert output['stacks'] == {'alice': ['z'], 'bob': []}
    assert output['processing'] == {'alice': set(), 'bob': set()}
    assert output['released'] == {'x'}


def test_validate_state():
    dsk = {'x': 1, 'y': (inc, 'x')}
    dependencies = {'x': set(), 'y': {'x'}}
    waiting = {'y': {'x'}, 'x': set()}
    dependents = {'x': {'y'}, 'y': set()}
    waiting_data = {'x': {'y'}}
    in_memory = set()
    stacks = {'alice': [], 'bob': []}
    processing = {'alice': set(), 'bob': set()}
    finished_results = set()
    released = set()

    validate_state(**locals())

    in_memory.add('x')
    with pytest.raises(Exception):
        validate_state(**locals())

    del waiting['x']
    with pytest.raises(Exception):
        validate_state(**locals())

    waiting['y'].remove('x')
    validate_state(**locals())

    stacks['alice'].append('y')
    with pytest.raises(Exception):
        validate_state(**locals())

    waiting.pop('y')
    validate_state(**locals())

    stacks['alice'].pop()
    with pytest.raises(Exception):
        validate_state(**locals())

    processing['alice'].add('y')
    validate_state(**locals())

    processing['alice'].pop()
    with pytest.raises(Exception):
        validate_state(**locals())

    in_memory.add('y')
    with pytest.raises(Exception):
        validate_state(**locals())

    finished_results.add('y')
    validate_state(**locals())


def test_rewind():
    """
        alpha  beta
          |     |
          x     y
         / \   / \ .
        a    b    c     d
        |    |    |     |
        A    B    C     D

    We have x and C, we lose b and D.  We'll need to recompute D, B and b.
    """
    dsk = {'A': 1, 'B': 2, 'C': 3, 'D': 4,
           'a': (inc, 'A'), 'b': (inc, 'B'), 'c': (inc, 'C'),
           'x': (add, 'a', 'b'), 'y': (add, 'b', 'c'),
           'alpha': (inc, 'x'), 'beta': (inc, 'y'), 'd': (inc, 'D')}
    dependencies, dependents = get_deps(dsk)
    waiting = {'alpha': {'x'}, 'beta': {'y'},
               'y': {'c'}}
    waiting_data = {'x': {'alpha'}, 'y': {'beta'},
                    'b': {'y'}, 'C': {'c'}}  # why is C here and not above?
    has_what = {'alice': {'x'}, 'bob': {'C'}}
    who_has = {'x': {'alice'}, 'C': {'bob'}}
    stacks = {'alice': ['alpha'], 'bob': ['c']}
    finished_results = {'d'}

    result = rewind(dependencies, dependents, waiting, waiting_data,
                    finished_results, stacks, who_has, 'b')

    e_waiting = {'alpha': {'x'}, 'beta': {'y'},
                'y': {'b', 'c'},
                'b': {'B'}}
    e_waiting_data = {'x': {'alpha'}, 'y': {'beta'},
                    'b': {'y'},
                    'B': {'b'}, 'C': {'c'}}

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert result == {'B': 'alice'} or result == {'B': 'bob'}

    result = rewind(dependencies, dependents, waiting, waiting_data,
                    finished_results, stacks, who_has, 'd')

    e_waiting = {'alpha': {'x'}, 'beta': 'y',
                'y': {'b', 'c'},
                'b': {'B'}, 'd': {'D'}}
    e_waiting_data = {'x': {'alpha'}, 'y': {'beta'},
                    'b': {'y'},
                    'B': {'b'}, 'C': {'c'}, 'D': {'d'},
                    'd': set()}

    assert waiting == e_waiting
    assert waiting_data == e_waiting_data
    assert finished_results == set()
    assert result == {'D': 'alice'} or result == {'D': 'bob'}


    """  Upon losing b we need to add it back into waiting_data for a
        b   c
         \ /
          a
    """
    dsk = {'a': 1, 'b': (inc, 'a'), 'c': (inc, 'a')}
    dependencies, dependents = get_deps(dsk)
    waiting = {}
    waiting_data = {'a': {'c'}, 'b': set(), 'c': set()}
    stacks = {'bob': ['c']}
    who_has = {'c': {'bob'}, 'a': {'bob'}}
    has_what = {'bob': {'a', 'c'}}
    finished_results = {'b'}

    result = rewind(dependencies, dependents, waiting, waiting_data,
                    finished_results, stacks, who_has, 'b')

    assert waiting_data == {'a': {'b', 'c'}, 'b': set(), 'c': set()}
    assert waiting == {}
    assert set(stacks['bob']) == {'b', 'c'}

    assert result == {'b': 'bob'}


def slowinc(x):
    from time import sleep
    sleep(0.1)
    print('slowinc', x)
    return x + 1


def run_center(port):
    from distributed3 import Center
    from tornado.ioloop import IOLoop
    center = Center('127.0.0.1', port)
    center.listen(port)
    IOLoop.current().start()
    IOLoop.current().close()


def run_worker(port, center_port, **kwargs):
    from distributed3 import Worker
    from tornado.ioloop import IOLoop
    worker = Worker('127.0.0.1', port, '127.0.0.1', center_port, **kwargs)
    worker.start()
    IOLoop.current().start()
    IOLoop.current().close()


@contextmanager
def cluster():
    center = Process(target=run_center, args=(8010,))
    a = Process(target=run_worker, args=(8011, 8010), kwargs={'ncores': 1})
    b = Process(target=run_worker, args=(8012, 8010), kwargs={'ncores': 1})

    center.start()
    a.start()
    b.start()

    sock = connect_sync('127.0.0.1', 8010)
    while True:
        write_sync(sock, {'op': 'ncores'})
        ncores = read_sync(sock)
        if len(ncores) == 2:
            break

    try:
        yield {'proc': center, 'port': 8010}, [{'proc': a, 'port': 8011},
                                               {'proc': b, 'port': 8012}]
    finally:
        for port in [8011, 8012, 8010]:
            with ignoring(socket.error):
                sock = connect_sync('127.0.0.1', port)
                write_sync(sock, dict(op='terminate', close=True))
                response = read_sync(sock)
                sock.close()
        for proc in [a, b, center]:
            with ignoring(Exception):
                proc.terminate()


def test_cluster():
    with cluster() as (c, [a, b]):
        pass


def test_failing_worker():
    n = 20
    dsk = {('x', i, j): (slowinc, ('x', i, j - 1)) for i in range(4)
                                                   for j in range(1, n)}
    dsk.update({('x', i, 0): i * 10 for i in range(4)})
    dsk['z'] = (sum, [('x', i, n - 1) for i in range(4)])
    keys = 'z'

    with cluster() as (c, [a, b]):
        def kill_a():
            sleep(0.5)
            a['proc'].terminate()

        @gen.coroutine
        def f():
            result = yield _get2('127.0.0.1', c['port'], dsk, keys)
            assert result == dask.get(dsk, keys)

        thread = Thread(target=kill_a)
        thread.start()
        IOLoop.current().run_sync(f)
