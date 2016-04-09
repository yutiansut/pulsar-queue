"""Tests task scheduling and execution"""
import unittest
import asyncio

from pulsar import send, multi_async
from pulsar.apps.test import dont_run_with_thread
from pulsar.apps import rpc

from pq import api

from tests import dummy


CODE_TEST = '''\
import time
def task_function(N = 10, lag = 0.1):
    time.sleep(lag)
    return N*N
'''


class TaskQueueBase(object):
    concurrency = 'thread'
    # used for both keep-alive and timeout in JsonProxy
    # long enough to allow to wait for tasks
    rpc_timeout = 500
    tq = None
    rpc = None

    @classmethod
    def name(cls):
        return cls.__name__.lower()

    @classmethod
    def task_backend(cls):
        return None

    @classmethod
    def rpc_name(cls):
        return 'rpc_%s' % cls.name()

    @classmethod
    async def setUpClass(cls):
        # The name of the task queue application
        name = cls.name()
        queues = ['%s1' % name, '%s2' % name]
        pq = api.PulsarQueue(cls.name(),
                             wsgi=True,
                             queue_callable=dummy,
                             task_queues=queues,
                             default_task_queue=queues[0],
                             rpc_bind='127.0.0.1:0',
                             concurrency=cls.concurrency,
                             rpc_concurrency=cls.concurrency,
                             rpc_keep_alive=cls.rpc_timeout,
                             task_paths=['tests.example.sampletasks.*'])
        cfgs = await pq.start()
        cls.tq = cfgs[0].app()
        cls.rpc = cfgs[1].app()
        # make sure the time out is high enough (bigger than test-timeout)
        cls.proxy = rpc.JsonProxy('http://%s:%s' % cls.rpc.cfg.addresses[0],
                                  timeout=cls.rpc_timeout)
        # Now flush the task queue
        backend = cls.tq.backend
        await backend.flush_queues(*queues)

    @classmethod
    def tearDownClass(cls):
        return multi_async((send('arbiter', 'kill_actor', a.name)
                           for a in (cls.tq, cls.rpc) if a is not None))


class TestTaskQueueOnThread(TaskQueueBase, unittest.TestCase):

    def test_registry(self):
        backend = self.tq.backend
        self.assertTrue(isinstance(backend.registry, dict))
        regular = backend.registry.regular()
        periodic = backend.registry.periodic()
        self.assertTrue(regular)
        self.assertTrue(periodic)

    def test_producer(self):
        backend = self.tq.backend
        self.assertTrue(str(backend).startswith('task producer <'))
        self.assertEqual(self.tq.cfg.default_task_queue, '%s1' % self.name())

    def test_job_list(self):
        jobs = self.tq.backend.job_list()
        self.assertTrue(jobs)
        self.assertTrue(isinstance(jobs, list))
        d = dict(jobs)
        pycode = d['runpycode']
        self.assertEqual(pycode['type'], 'regular')

    async def test_simple_task(self):
        task = await self.tq.queue_task('addition', a=40, b=50)
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertEqual(task.result, 90)
        self.assertTrue(str(task).startswith('task.addition<'))
        self.assertTrue(task.done())

    async def test_simple_revoked(self):
        task = await self.tq.queue_task('addition', a=40, b=50, expiry=0)
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'REVOKED')
        self.assertFalse(task.result)

    async def test_info(self):
        task = await self.tq.queue_task('workerinfo')
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertIsInstance(task.result, dict)
        self.assertEqual(len(task.result['queues']), 3)

    async def test_async_job(self):
        result = self.tq.queue_task('asynchronous', lag=2)
        self.assertIsInstance(result, asyncio.Future)
        task = await result
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertTrue(task.result >= 2)

    async def test_failure(self):
        task = await self.tq.queue_task('testperiodicerror', msg='testing')
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'FAILURE')
        self.assertEqual(task.result, 'testing')
        self.assertTrue(task.stacktrace)

    async def test_execute_addition(self):
        future = self.tq.execute_task('addition', a=3, b=-4)
        self.assertIsInstance(future, api.TaskFuture)
        self.assertTrue(future.task_id)
        task = await future
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertEqual(task.result, -1)
        self.assertFalse(task.worker)
        self.assertFalse(task.queue)

    async def test_green_executor(self):
        task = await self.tq.queue_task('greenexecutor')
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertIsInstance(task.result, dict)
        self.assertEqual(len(task.result['queues']), 3)

    async def test_local_queue(self):
        backend = self.tq.backend
        task = await backend.queue_task_local('testlocalqueue')
        self.assertIsInstance(task, api.Task)
        self.assertIsInstance(task.result, list)
        self.assertEqual(len(task.result), 3)
        self.assertEqual(task.result[0], backend.node_name)

    async def test_no_callback(self):
        backend = self.tq.backend
        task = await backend.queue_task('asynchronous', callback=False)
        self.assertTrue(task.id)
        self.assertEqual(task.status_string, 'QUEUED')


@dont_run_with_thread
class TestTaskQueueOnProcess(TestTaskQueueOnThread):
    concurrency = 'process'

    async def test_cpubound_task(self):
        task = await self.tq.queue_task('cpubound')
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertEqual(task.result, ['OK', 2])

    async def __test_error_cpubound_task(self):
        task = await self.tq.queue_task('cpubound', error=True)
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'FAILURE')
        self.assertTrue(task.stacktrace)

    async def test_is_in_greenlet(self):
        task = await self.tq.queue_task('cpuboundwithasync')
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        self.assertEqual(task.result, True)

    async def test_supports_asyncio(self):
        task = await self.tq.queue_task('cpuboundwithasync', asyncio=True)
        self.assertIsInstance(task, api.Task)
        self.assertEqual(task.status_string, 'SUCCESS')
        # If the task is asyncio it drops out of the greenlet
        self.assertEqual(task.result, False)

    async def test_big_log(self):
        # If this test fails, this is because the test runner will timeout on
        # this future, this is because the pipe fills up and blocks the
        # cpu bound task
        await self.tq.queue_task('cpuboundbiglog')