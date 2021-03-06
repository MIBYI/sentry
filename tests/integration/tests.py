# -*- coding: utf-8 -*-

from __future__ import absolute_import, print_function

import os
import datetime
import json
import logging
import mock
import six
import zlib

from sentry import tagstore
from django.conf import settings
from django.core.urlresolvers import reverse
from django.test.utils import override_settings
from django.utils import timezone
from exam import fixture
from gzip import GzipFile
from sentry_sdk import Hub, Client
from six import StringIO

from sentry.models import (Group, Event)
from sentry.testutils import TestCase, TransactionTestCase
from sentry.testutils.helpers import get_auth_header
from sentry.utils.settings import (validate_settings, ConfigurationError, import_string)

DEPENDENCY_TEST_DATA = {
    "postgresql": (
        'DATABASES', 'psycopg2.extensions', "database engine",
        "django.db.backends.postgresql_psycopg2", {
            'default': {
                'ENGINE': "django.db.backends.postgresql_psycopg2",
                'NAME': 'test',
                'USER': 'root',
                'PASSWORD': '',
                'HOST': 'localhost',
                'PORT': ''
            }
        }
    ),
    "mysql": (
        'DATABASES', 'MySQLdb', "database engine", "django.db.backends.mysql", {
            'default': {
                'ENGINE': "django.db.backends.mysql",
                'NAME': 'test',
                'USER': 'root',
                'PASSWORD': '',
                'HOST': 'localhost',
                'PORT': ''
            }
        }
    ),
    "oracle": (
        'DATABASES', 'cx_Oracle', "database engine", "django.db.backends.oracle", {
            'default': {
                'ENGINE': "django.db.backends.oracle",
                'NAME': 'test',
                'USER': 'root',
                'PASSWORD': '',
                'HOST': 'localhost',
                'PORT': ''
            }
        }
    ),
    "memcache": (
        'CACHES', 'memcache', "caching backend",
        "django.core.cache.backends.memcached.MemcachedCache", {
            'default': {
                'BACKEND': "django.core.cache.backends.memcached.MemcachedCache",
                'LOCATION': '127.0.0.1:11211',
            }
        }
    ),
    "pylibmc": (
        'CACHES', 'pylibmc', "caching backend", "django.core.cache.backends.memcached.PyLibMCCache",
        {
            'default': {
                'BACKEND': "django.core.cache.backends.memcached.PyLibMCCache",
                'LOCATION': '127.0.0.1:11211',
            }
        }
    ),
}


def get_fixture_path(name):
    return os.path.join(os.path.dirname(__file__), 'fixtures', name)


def load_fixture(name):
    with open(get_fixture_path(name)) as fp:
        return fp.read()


class RavenIntegrationTest(TransactionTestCase):
    """
    This mocks the test server and specifically tests behavior that would
    happen between Raven <--> Sentry over HTTP communication.
    """

    def setUp(self):
        self.user = self.create_user('coreapi@example.com')
        self.project = self.create_project()
        self.pk = self.project.key_set.get_or_create()[0]

        self.configure_sentry_errors()

    def configure_sentry_errors(self):
        # delay raising of assertion errors to make sure they do not get
        # swallowed again
        failures = []

        class AssertHandler(logging.Handler):
            def emit(self, entry):
                failures.append(entry)

        assert_handler = AssertHandler()

        for name in 'sentry.errors', 'sentry_sdk.errors':
            sentry_errors = logging.getLogger(name)
            sentry_errors.addHandler(assert_handler)
            sentry_errors.setLevel(logging.DEBUG)

            @self.addCleanup
            def remove_handler(sentry_errors=sentry_errors):
                sentry_errors.handlers.pop(sentry_errors.handlers.index(assert_handler))

        @self.addCleanup
        def reraise_failures():
            for entry in failures:
                raise AssertionError(entry.message)

    def send_event(self, method, url, body, headers):
        from sentry.app import buffer

        with self.tasks():
            content_type = headers.pop('Content-Type', None)
            headers = {'HTTP_' + k.replace('-', '_').upper(): v for k, v in six.iteritems(headers)}
            resp = self.client.post(
                reverse(
                    'sentry-api-store',
                    args=[self.pk.project_id],
                ),
                data=body,
                content_type=content_type,
                **headers
            )
            assert resp.status_code == 200

            buffer.process_pending()

    @mock.patch('urllib3.PoolManager.request')
    def test_basic(self, request):
        requests = []

        def queue_event(method, url, body, headers):
            requests.append((method, url, body, headers))

        request.side_effect = queue_event

        hub = Hub(Client(
            'http://%s:%s@localhost:8000/%s' %
            (self.pk.public_key, self.pk.secret_key, self.pk.project_id),
            default_integrations=False
        ))

        hub.capture_message('foo')
        hub.client.close()

        for _request in requests:
            self.send_event(*_request)

        assert request.call_count is 1
        assert Group.objects.count() == 1
        group = Group.objects.get()
        assert group.event_set.count() == 1
        instance = group.event_set.get()
        assert instance.data['logentry']['formatted'] == 'foo'


class SentryRemoteTest(TestCase):
    @fixture
    def path(self):
        return reverse('sentry-api-store')

    def test_minimal(self):
        kwargs = {'message': 'hello', 'tags': {'foo': 'bar'}}

        resp = self._postWithHeader(kwargs)

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)
        Event.objects.bind_nodes([instance], 'data')

        assert instance.message == 'hello'
        assert instance.data['logentry'] == {'formatted': 'hello'}
        assert instance.title == instance.data['title'] == 'hello'
        assert instance.location is instance.data['location'] is None

        assert tagstore.get_tag_key(self.project.id, None, 'foo') is not None
        assert tagstore.get_tag_value(self.project.id, None, 'foo', 'bar') is not None
        assert tagstore.get_group_tag_key(
            self.project.id, instance.group_id, None, 'foo') is not None
        assert tagstore.get_group_tag_value(
            instance.project_id,
            instance.group_id,
            None,
            'foo',
            'bar') is not None

    def test_exception(self):
        kwargs = {'exception': {
            'type': 'ZeroDivisionError',
            'value': 'cannot divide by zero',
            'stacktrace': {'frames': [
                {
                    'filename': 'utils.py',
                    'in_app': False,
                    'function': 'raise_it',
                    'module': 'utils',
                },
                {
                    'filename': 'main.py',
                    'in_app': True,
                    'function': 'fail_it',
                    'module': 'main',
                }
            ]}
        }, 'tags': {'foo': 'bar'}}

        resp = self._postWithHeader(kwargs)

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)
        Event.objects.bind_nodes([instance], 'data')

        assert len(instance.data['exception']) == 1
        assert instance.title == instance.data['title'] == 'ZeroDivisionError: cannot divide by zero'
        assert instance.location == instance.data['location'] == 'main.py'
        assert instance.culprit == instance.data['culprit'] == 'main in fail_it'

        assert tagstore.get_tag_key(self.project.id, None, 'foo') is not None
        assert tagstore.get_tag_value(self.project.id, None, 'foo', 'bar') is not None
        assert tagstore.get_group_tag_key(
            self.project.id, instance.group_id, None, 'foo') is not None
        assert tagstore.get_group_tag_value(
            instance.project_id,
            instance.group_id,
            None,
            'foo',
            'bar') is not None

    def test_timestamp(self):
        timestamp = timezone.now().replace(
            microsecond=0, tzinfo=timezone.utc
        ) - datetime.timedelta(hours=1)
        kwargs = {u'message': 'hello', 'timestamp': float(timestamp.strftime('%s.%f'))}
        resp = self._postWithSignature(kwargs)
        assert resp.status_code == 200, resp.content
        instance = Event.objects.get()
        assert instance.message == 'hello'
        assert instance.datetime == timestamp
        group = instance.group
        assert group.first_seen == timestamp
        assert group.last_seen == timestamp

    def test_timestamp_as_iso(self):
        timestamp = timezone.now().replace(
            microsecond=0, tzinfo=timezone.utc
        ) - datetime.timedelta(hours=1)
        kwargs = {u'message': 'hello', 'timestamp': timestamp.strftime('%Y-%m-%dT%H:%M:%S.%f')}
        resp = self._postWithSignature(kwargs)
        assert resp.status_code == 200, resp.content
        instance = Event.objects.get()
        assert instance.message == 'hello'
        assert instance.datetime == timestamp
        group = instance.group
        assert group.first_seen == timestamp
        assert group.last_seen == timestamp

    def test_ungzipped_data(self):
        kwargs = {'message': 'hello'}
        resp = self._postWithSignature(kwargs)
        assert resp.status_code == 200
        instance = Event.objects.get()
        assert instance.message == 'hello'

    @override_settings(SENTRY_ALLOW_ORIGIN='sentry.io')
    def test_correct_data_with_get(self):
        kwargs = {'message': 'hello'}
        resp = self._getWithReferer(kwargs)
        assert resp.status_code == 200, resp.content
        instance = Event.objects.get()
        assert instance.message == 'hello'

    @override_settings(SENTRY_ALLOW_ORIGIN='*')
    def test_get_without_referer_allowed(self):
        self.project.update_option('sentry:origins', '')
        kwargs = {'message': 'hello'}
        resp = self._getWithReferer(kwargs, referer=None, protocol='4')
        assert resp.status_code == 200, resp.content

    @override_settings(SENTRY_ALLOW_ORIGIN='sentry.io')
    def test_correct_data_with_post_referer(self):
        kwargs = {'message': 'hello'}
        resp = self._postWithReferer(kwargs)
        assert resp.status_code == 200, resp.content
        instance = Event.objects.get()
        assert instance.message == 'hello'

    @override_settings(SENTRY_ALLOW_ORIGIN='sentry.io')
    def test_post_without_referer(self):
        self.project.update_option('sentry:origins', '')
        kwargs = {'message': 'hello'}
        resp = self._postWithReferer(kwargs, referer=None, protocol='4')
        assert resp.status_code == 200, resp.content

    @override_settings(SENTRY_ALLOW_ORIGIN='*')
    def test_post_without_referer_allowed(self):
        self.project.update_option('sentry:origins', '')
        kwargs = {'message': 'hello'}
        resp = self._postWithReferer(kwargs, referer=None, protocol='4')
        assert resp.status_code == 200, resp.content

    @override_settings(SENTRY_ALLOW_ORIGIN='google.com')
    def test_post_with_invalid_origin(self):
        self.project.update_option('sentry:origins', 'sentry.io')
        kwargs = {'message': 'hello'}
        resp = self._postWithReferer(
            kwargs,
            referer='https://getsentry.net',
            protocol='4'
        )
        assert resp.status_code == 403, resp.content

    def test_signature(self):
        kwargs = {'message': 'hello'}

        resp = self._postWithSignature(kwargs)

        assert resp.status_code == 200, resp.content

        instance = Event.objects.get()

        assert instance.message == 'hello'

    def test_content_encoding_deflate(self):
        kwargs = {'message': 'hello'}

        message = zlib.compress(json.dumps(kwargs))

        key = self.projectkey.public_key
        secret = self.projectkey.secret_key

        with self.tasks():
            resp = self.client.post(
                self.path,
                message,
                content_type='application/octet-stream',
                HTTP_CONTENT_ENCODING='deflate',
                HTTP_X_SENTRY_AUTH=get_auth_header('_postWithHeader', key, secret),
            )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'

    def test_content_encoding_gzip(self):
        kwargs = {'message': 'hello'}

        message = json.dumps(kwargs)

        fp = StringIO()

        try:
            f = GzipFile(fileobj=fp, mode='w')
            f.write(message)
        finally:
            f.close()

        key = self.projectkey.public_key
        secret = self.projectkey.secret_key

        with self.tasks():
            resp = self.client.post(
                self.path,
                fp.getvalue(),
                content_type='application/octet-stream',
                HTTP_CONTENT_ENCODING='gzip',
                HTTP_X_SENTRY_AUTH=get_auth_header('_postWithHeader', key, secret),
            )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'

    def test_protocol_v2_0_without_secret_key(self):
        kwargs = {'message': 'hello'}

        resp = self._postWithHeader(
            data=kwargs,
            key=self.projectkey.public_key,
            protocol='2.0',
        )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'

    def test_protocol_v3(self):
        kwargs = {'message': 'hello'}

        resp = self._postWithHeader(
            data=kwargs,
            key=self.projectkey.public_key,
            secret=self.projectkey.secret_key,
            protocol='3',
        )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'

    def test_protocol_v4(self):
        kwargs = {'message': 'hello'}

        resp = self._postWithHeader(
            data=kwargs,
            key=self.projectkey.public_key,
            secret=self.projectkey.secret_key,
            protocol='4',
        )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'

    def test_protocol_v5(self):
        kwargs = {'message': 'hello'}

        resp = self._postWithHeader(
            data=kwargs,
            key=self.projectkey.public_key,
            secret=self.projectkey.secret_key,
            protocol='5',
        )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'

    def test_protocol_v6(self):
        kwargs = {'message': 'hello'}

        resp = self._postWithHeader(
            data=kwargs,
            key=self.projectkey.public_key,
            secret=self.projectkey.secret_key,
            protocol='6',
        )

        assert resp.status_code == 200, resp.content

        event_id = json.loads(resp.content)['id']
        instance = Event.objects.get(event_id=event_id)

        assert instance.message == 'hello'


class DependencyTest(TestCase):
    def raise_import_error(self, package):
        def callable(package_name):
            if package_name != package:
                return import_string(package_name)
            raise ImportError("No module named %s" % (package, ))

        return callable

    @mock.patch('django.conf.settings', mock.Mock())
    @mock.patch('sentry.utils.settings.import_string')
    def validate_dependency(
        self, key, package, dependency_type, dependency, setting_value, import_string
    ):

        import_string.side_effect = self.raise_import_error(package)

        with self.settings(**{key: setting_value}):
            with self.assertRaises(ConfigurationError):
                validate_settings(settings)

    def test_validate_fails_on_postgres(self):
        self.validate_dependency(*DEPENDENCY_TEST_DATA['postgresql'])

    def test_validate_fails_on_mysql(self):
        self.validate_dependency(*DEPENDENCY_TEST_DATA['mysql'])

    def test_validate_fails_on_oracle(self):
        self.validate_dependency(*DEPENDENCY_TEST_DATA['oracle'])

    def test_validate_fails_on_memcache(self):
        self.validate_dependency(*DEPENDENCY_TEST_DATA['memcache'])

    def test_validate_fails_on_pylibmc(self):
        self.validate_dependency(*DEPENDENCY_TEST_DATA['pylibmc'])


def get_fixtures(name):
    path = os.path.join(os.path.dirname(__file__), 'fixtures/csp', name)
    try:
        with open(path + '_input.json', 'rb') as fp1:
            input = fp1.read()
    except IOError:
        input = None

    try:
        with open(path + '_output.json', 'rb') as fp2:
            output = json.load(fp2)
    except IOError:
        output = None

    return input, output


class CspReportTest(TestCase):
    def assertReportCreated(self, input, output):
        resp = self._postCspWithHeader(input)
        assert resp.status_code == 201, resp.content
        assert Event.objects.count() == 1
        e = Event.objects.all()[0]
        Event.objects.bind_nodes([e], 'data')
        assert output['message'] == e.data['logentry']['formatted']
        for key, value in six.iteritems(output['tags']):
            assert e.get_tag(key) == value
        for key, value in six.iteritems(output['data']):
            assert e.data[key] == value

    def assertReportRejected(self, input):
        resp = self._postCspWithHeader(input)
        assert resp.status_code in (400, 403), resp.content

    def test_chrome_blocked_asset(self):
        self.assertReportCreated(*get_fixtures('chrome_blocked_asset'))

    def test_firefox_missing_effective_uri(self):
        input, _ = get_fixtures('firefox_blocked_asset')
        self.assertReportRejected(input)
