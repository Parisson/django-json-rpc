import datetime, decimal
import sys
from functools import wraps
from uuid import uuid1
from jsonrpc._json import loads, dumps
from jsonrpc.exceptions import *
from jsonrpc._types import *
from django.conf import settings
from django.core import signals
from django.utils.encoding import smart_str
empty_dec = lambda f: f
try:
    from django.views.decorators.csrf import csrf_exempt
except (NameError, ImportError):
    csrf_exempt = empty_dec

from django.core.serializers.json import DjangoJSONEncoder

NoneType = type(None)
encode_kw = lambda p: dict([(str(k), v) for k, v in p.items()])


def trim_docstring(docstring):
    if not docstring:
        return ''
    # Convert tabs to spaces (following the normal Python rules)
    # and split into a list of lines:
    lines = docstring.expandtabs().splitlines()
    # Determine minimum indentation (first line doesn't count):
    indent = sys.maxsize
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped:
            indent = min(indent, len(line) - len(stripped))
    # Remove indentation (first line is special):
    trimmed = [lines[0].strip()]
    if indent < sys.maxsize:
        for line in lines[1:]:
            trimmed.append(line[indent:].rstrip())
    # Strip off trailing and leading blank lines:
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    while trimmed and not trimmed[0]:
        trimmed.pop(0)
    # Return a single string:
    return '\n'.join(trimmed)


def encode_kw11(p):
    if not type(p) is dict:
        return {}
    ret = p.copy()
    removes = []
    for k, v in ret.items():
        try:
            int(k)
        except ValueError:
            pass
        else:
            removes.append(k)
    for k in removes:
        ret.pop(k)
    return ret


def encode_arg11(p):
    if type(p) is list:
        return p
    elif not type(p) is dict:
        return []
    else:
        pos = []
        d = encode_kw(p)
        for k, v in d.items():
            try:
                pos.append(int(k))
            except ValueError:
                pass
        pos = list(set(pos))
        pos.sort()
        return [d[str(i)] for i in pos]


def validate_params(method, D):
    if type(D['params']) == Object:
        keys = list(method.json_arg_types.keys())
        if len(keys) != len(D['params']):
            raise InvalidParamsError('Not enough params provided for %s' %
                                     method.json_sig)
        for k in keys:
            if not k in D['params']:
                print('\n\n\n\nSHITTER SHITTER', k, D, '\n\n\n\n')
                raise InvalidParamsError('%s is not a valid parameter for %s' %
                                         (k, method.json_sig))
            if not Any.kind(D['params'][k]) == method.json_arg_types[k]:
                raise InvalidParamsError(
                    '%s is not the correct type %s for %s' %
                    (type(D['params'][k]), method.json_arg_types[k],
                     method.json_sig))
    elif type(D['params']) == Array:
        arg_types = list(method.json_arg_types.values())
        try:
            for i, arg in enumerate(D['params']):
                if not Any.kind(arg) == arg_types[i]:
                    raise InvalidParamsError(
                        '%s is not the correct type %s for %s' %
                        (type(arg), arg_types[i], method.json_sig))
        except IndexError:
            raise InvalidParamsError('Too many params provided for %s' %
                                     method.json_sig)
        else:
            if len(D['params']) != len(arg_types):
                raise InvalidParamsError('Not enough params provided for %s' %
                                         method.json_sig)


class JSONRPCSite(object):
    "A JSON-RPC Site"

    def __init__(self, json_encoder=DjangoJSONEncoder):
        self.urls = {}
        self.uuid = str(uuid1())
        self.version = '1.0'
        self.name = 'django-json-rpc'
        self.register('system.describe', self.describe)
        self.set_json_encoder(json_encoder)

    def set_json_encoder(self, json_encoder=DjangoJSONEncoder):
        self.json_encoder = json_encoder

    def register(self, name, method):
        self.urls[smart_str(name)] = method

    def empty_response(self, version='1.0'):
        resp = {'id': None}
        if version == '1.1':
            resp['version'] = version
            return resp
        if version == '2.0':
            resp['jsonrpc'] = version
        resp.update({'error': None, 'result': None})
        return resp

    def validate_get(self, request, method):
        encode_get_params = lambda r: dict([(k, v[0] if len(v) == 1 else v) for k, v in r])
        if request.method == 'GET':
            method = smart_str(method)
            if method in self.urls and getattr(self.urls[method], 'json_safe',
                                                   False):
                D = {
                    'params': encode_get_params(request.GET.lists()),
                    'method': method,
                    'id': 'jsonrpc',
                    'version': '1.1'
                }
                return True, D
        return False, {}

    def response_dict(self, request, D,
                      is_batch=False,
                      version_hint='1.0',
                      json_encoder=None):
        json_encoder = json_encoder or self.json_encoder
        version = version_hint
        response = self.empty_response(version=version)
        apply_version = {
            '2.0':
            lambda f, r, p: f(r, **encode_kw(p)) if type(p) is dict else f(r, *p),
            '1.1':
            lambda f, r, p: f(r, *encode_arg11(p), **encode_kw(encode_kw11(p))),
            '1.0': lambda f, r, p: f(r, *p)
        }

        try:
            # params: An Array or Object, that holds the actual parameter values
            # for the invocation of the procedure. Can be omitted if empty.
            if 'params' not in D:
                D['params'] = []
            if 'method' not in D or 'params' not in D:
                raise InvalidParamsError(
                    'Request requires str:"method" and list:"params"')
            if D['method'] not in self.urls:
                raise MethodNotFoundError(
                    'Method not found. Available methods: %s' % (
                        '\n'.join(self.urls.keys())))

            if 'jsonrpc' in D:
                if str(D['jsonrpc']) not in apply_version:
                    raise InvalidRequestError(
                        'JSON-RPC version %s not supported.' % D['jsonrpc'])
                version = request.jsonrpc_version = response['jsonrpc'] = str(
                    D['jsonrpc'])
            elif 'version' in D:
                if str(D['version']) not in apply_version:
                    raise InvalidRequestError(
                        'JSON-RPC version %s not supported.' % D['version'])
                version = request.jsonrpc_version = response['version'] = str(
                    D['version'])
            else:
                request.jsonrpc_version = '1.0'

            method = self.urls[str(D['method'])]
            if getattr(method, 'json_validate', False):
                validate_params(method, D)

            if 'id' in D and D['id'] is not None:  # regular request
                response['id'] = D['id']
                if version in ('1.1', '2.0') and 'error' in response:
                    response.pop('error')
            elif is_batch:  # notification, not ok in a batch format, but happened anyway
                raise InvalidRequestError

            R = apply_version[version](method, request, D['params'])

            if 'id' not in D or ('id' in D and D['id'] is None):  # notification
                return None, 204

            if isinstance(R, tuple):
                R = list(R)

            encoder = json_encoder()
            builtin_types = (dict, list, set, NoneType, bool, six.text_type
                       ) + six.integer_types + six.string_types
            if all(not isinstance(R, e) for e in builtin_types):
                try:
                    rs = encoder.default(R)  # ...or something this thing supports
                except TypeError as exc:
                    raise TypeError("Return type not supported, for %r" % R)

            response['result'] = R

            status = 200

        except Error as e:
            response['error'] = e.json_rpc_format
            if version in ('1.1', '2.0') and 'result' in response:
                response.pop('result')
            status = e.status
        except Exception as e:
            # exception missed by others
            signals.got_request_exception.send(sender=self.__class__,
                                               request=request)

            # Put stacktrace into the OtherError only if DEBUG is enabled
            if settings.DEBUG:
                other_error = OtherError(e)
            else:
                other_error = OtherError("Internal Server Error")

            response['error'] = other_error.json_rpc_format
            status = other_error.status
            if version in ('1.1', '2.0') and 'result' in response:
                response.pop('result')

        # Exactly one of result or error MUST be specified. It's not
        # allowed to specify both or none.
        if version in ('1.1', '2.0'
                   ) and 'error' in response and not response['error']:
            response.pop('error')

        return response, status

    @csrf_exempt
    def dispatch(self, request, method='', json_encoder=None):
        from django.http import HttpResponse
        json_encoder = json_encoder or self.json_encoder

        try:
            # in case we do something json doesn't like, we always get back valid json-rpc response
            response = self.empty_response()
            if request.method.lower() == 'get':
                valid, D = self.validate_get(request, method)
                if not valid:
                    raise InvalidRequestError(
                        'The method you are trying to access is '
                        'not available by GET requests')
            elif not request.method.lower() == 'post':
                raise RequestPostError
            else:
                try:
                    if hasattr(request, "body"):
                        D = loads(request.body.decode('utf-8'))
                    else:
                        D = loads(request.raw_post_data.decode('utf-8'))
                except:
                    raise InvalidRequestError

            if type(D) is list:
                response = [self.response_dict(request, d,
                                               is_batch=True,
                                               json_encoder=json_encoder)[0]
                            for d in D]
                status = 200
            else:
                response, status = self.response_dict(
                    request, D,
                    json_encoder=json_encoder)
                if response is None and (not 'id' in D or D['id'] is None):  # a notification
                    return HttpResponse('', status=status)

            json_rpc = dumps(response, cls=json_encoder)
        except Error as e:
            response['error'] = e.json_rpc_format
            status = e.status
            json_rpc = dumps(response, cls=json_encoder)
        except Exception as e:
            # exception missed by others
            signals.got_request_exception.send(sender=self.__class__,
                                               request=request)

            # Put stacktrace into the OtherError only if DEBUG is enabled
            if settings.DEBUG:
                other_error = OtherError(e)
            else:
                other_error = OtherError("Internal Server Error")

            response['result'] = None
            response['error'] = other_error.json_rpc_format
            status = other_error.status

            json_rpc = dumps(response, cls=json_encoder)

        return HttpResponse(json_rpc,
                            status=status,
                            content_type='application/json-rpc')

    def procedure_desc(self, key):
        M = self.urls[key]
        return {
            'name': M.json_method,
            'summary': trim_docstring(M.__doc__),
            'idempotent': M.json_safe,
            'params': [{'type': str(Any.kind(t)),
                        'name': k} for k, t in M.json_arg_types.items()],
            'return': {'type': str(M.json_return_type)}
        }

    def service_desc(self):
        return {
            'sdversion': '1.0',
            'name': self.name,
            'id': 'urn:uuid:%s' % str(self.uuid),
            'summary': trim_docstring(self.__doc__),
            'version': self.version,
            'procs': [self.procedure_desc(k) for k in self.urls.keys()
                      if self.urls[k] != self.describe]
        }

    def describe(self, request):
        return self.service_desc()


jsonrpc_site = JSONRPCSite()
