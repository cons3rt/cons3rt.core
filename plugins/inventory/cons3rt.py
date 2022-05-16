from __future__ import (absolute_import, division, print_function)

__metaclass__ = type

import json
import requests
from requests.exceptions import RequestException, SSLError
from .requests_pkcs12 import Pkcs12Adapter
import concurrent.futures
import time
from ansible.errors import AnsibleError
from ansible.plugins.inventory import BaseInventoryPlugin
from ansible.plugins.inventory import Cacheable
from ansible.plugins.inventory import Constructable
from ansible.template import Templar
from ansible.module_utils.common.dict_transformations import camel_dict_to_snake_dict


DOCUMENTATION = '''
    name: cons3rt
    short_description: cons3rt inventory source
    extends_documentation_fragment:
    - inventory_cache
    - constructed
    
    description:
        - Get inventory hosts from a CONS3RT environment.
        - Uses a YAML configuration file that ends with C(cons3rt.{yml|yaml}).
    author:
        - Todd Fisher (@togofish)
    options:
        plugin:
            description: Token that ensures this is a source file for the plugin.
            required: True
            choices: ['cons3rt','cons3rt.core.cons3rt']
        cons3rt_url:
            description: Base url of the CONS3RT api (e.g., https://api.arcus-cloud.io/rest)
            required: True
        cert_file_path:
            description: Path to the certificate file (P12)
            required: True
        cons3rt_token:
            description: CONS3RT API project token for the user
            required: True
        cert_password:
            description: Password for the certificate file
            required: True
'''


class Cons3rtClientError(Exception):
    """There was a problem setting up a CONS3RT client"""


class Session:
    """A session to the CONS3RT API
    :param string base: base url of the cons3rt api (e.g., https://api.cons3rt.com/rest)
    :param dict credentials: a dict containing the credentials to use for authentication. cert_file_path, cert_password,
     and cons3rt_token are required.
    """
    def __init__(self, base=None, credentials=None,):
        self.max_retry_attempts = 10
        self.retry_time_sec = 5
        self.base = base
        self.credentials = credentials

    @staticmethod
    def validate_target(target):
        """
        Validates that a target was provided and is a string
        :param target: the target url for the http request
        :return: void
        :raises: Cons3rtClientError
        """
        if target is None or not isinstance(target, str):
            raise Cons3rtClientError('Invalid target arg provided')

    def http_get(self, target):
        """
        Runs an HTTP GET request to the CONS3RT ReST API
        :param target: (str) URL
        :return: http response
        """

        self.validate_target(target)

        # Set the URL
        url = self.base + target

        attempt_num = 1
        err_msg_tally = ''
        while True:
            if attempt_num >= self.max_retry_attempts:
                msg = 'Max attempts exceeded: {n}\n{e}'.format(n=str(self.max_retry_attempts), e=err_msg_tally)
                raise Cons3rtClientError(msg)
            err_msg = ''
            with requests.Session() as s:
                s.mount(self.base, Pkcs12Adapter(pkcs12_filename=self.credentials['cert_file_path'],
                                                 pkcs12_password=self.credentials['cert_password']))
                user_header = {
                    'token': self.credentials['cons3rt_token'],
                    'Accept': 'application/json'
                }
                s.headers.update(user_header)
            try:
                response = s.get(url)
            except RequestException as exc:
                err_msg += 'RequestException on GET attempt #{n}\n{e}'.format(n=str(attempt_num), e=str(exc))
                print(err_msg)
            except SSLError as exc:
                err_msg += 'SSLError on GET attempt #{n}\n{e}'.format(n=str(attempt_num), e=str(exc))
                print(err_msg)
            else:
                return response
            err_msg_tally += err_msg + '\n'

            attempt_num += 1
            time.sleep(self.retry_time_sec)

    @staticmethod
    def parse_response(response):

        # Determine is there is content and if it needs to be decoded
        if response.content:
            if isinstance(response.content, bytes):
                decoded_content = response.content.decode('utf-8')
            else:
                decoded_content = response.content
        else:
            decoded_content = None

        # Raise an exception if a bad HTTP code was received
        if response.status_code not in [requests.codes.ok, 202]:
            msg = 'Received HTTP code [{n}] with headers:\n{h}'.format(
                n=str(response.status_code), h=response.headers)
            if decoded_content:
                msg += '\nand content:\n{c}'.format(c=decoded_content)
            raise Cons3rtClientError(msg)

        # Return the decoded content
        if response.status_code == requests.codes.ok:
            print('Received an OK HTTP Response Code')
        elif response.status_code == 202:
            print('Received an ACCEPTED HTTP Response Code (202)')
        print('Parsed decoded content: {c}'.format(c=decoded_content))
        return decoded_content

    def get_drs(self):
        deployment_runs = json.loads(self.http_get('/api/drs?search_type=SEARCH_AVAILABLE&in_project=true')
                                     .content.decode('utf-8'))
        return deployment_runs

    def get_dr_hosts(self, deployment_run_id):
        deployment_run = json.loads(self.http_get("/api/drs/" + str(deployment_run_id)).content.decode('utf-8'))
        for d in deployment_run['deploymentRunHosts']:
            d.update({'drId': deployment_run_id})
            d.update({'drName': deployment_run['name']})
        return deployment_run['deploymentRunHosts']

    def get_dr_host_details(self, deployment_run_id, deployment_run_host_id, deployment_run_name):
        deployment_run_host = json.loads(self.http_get("/api/drs/" + str(deployment_run_id) + "/host/" +
                                                       str(deployment_run_host_id)).content.decode('utf-8'))
        deployment_run_host.update({'drId': deployment_run_id})
        deployment_run_host.update({'drName': deployment_run_name})
        return deployment_run_host


class InventoryModule(BaseInventoryPlugin, Constructable, Cacheable):
    NAME = 'cons3rt'

    def __init__(self):
        super(InventoryModule, self).__init__()

        self.group_prefix = 'cons3rt_'
        self.max_workers = 10

        self.cons3rt_url = None

        # credentials
        self.cert_file_path = None
        self.cons3rt_token = None
        self.cert_password = None

    def _get_connection(self, credentials):
        try:
            connection = Session(self.cons3rt_url, credentials)
        except Cons3rtClientError as e:
            raise AnsibleError("Unable to create a CONS3RT session: %s" % str(e))
        return connection

    def _get_hosts(self):

        start = time.perf_counter()
        all_dr_hosts = []
        dr_hosts = []

        credentials = self._get_credentials()
        client = self._get_connection(credentials)

        drs = client.get_drs()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            results = []
            for dr in drs:
                if dr['fapStatus'] == "RESERVED":
                    results.append(executor.submit(client.get_dr_hosts, dr['id']))

        for r in results:
            dr_hosts.extend(r.result())

        print(f'Time to get DR list: {time.perf_counter() - start}')

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            results = []
            for dr_host in dr_hosts:
                results.append(executor.submit(client.get_dr_host_details, dr_host['drId'], dr_host['id'],
                                               dr_host['drName']))

        for r in results:
            all_dr_hosts.append(r.result())

        return all_dr_hosts

    def _query(self):
        """
        Queries CONS3RT for deployment run hosts. We can set up some filtering here in the future
        :return: all deployment run hosts in a project that are available and reserved in fap (i.e., no errors)
        """

        hosts = self._get_hosts()
        hosts = sorted(hosts, key=lambda x: x['id'])
        return {'cons3rt': hosts}

    def _populate(self, groups):
        for group in groups:
            group = self.inventory.add_group(group)
            self._add_hosts(hosts=groups[group], group=group)
            self.inventory.add_child('all', group)

    def _add_hosts(self, hosts, group):
        """
        Adds the hosts to the inventory and adds them to the group
        :param hosts: a list of hosts to be added to a group
        :param group: the name of the group to which the hosts belong
        """
        for host in hosts:
            hostname = host['systemRole']

            host = camel_dict_to_snake_dict(host, ignore_list=['Tags'])

            if not hostname:
                continue
            self.inventory.add_host(hostname, group=group)
            new_vars = dict()
            for hostvar, hostval in host.items():
                new_vars[hostvar] = hostval
                self.inventory.set_variable(hostname, hostvar, hostval)
            host.update(new_vars)

            # Composed variables
            self._set_composite_vars(self.get_option('compose'), host, hostname)

            # Complex groups based on jinja2 conditionals, hosts that meet the conditional are added to group
            self._add_host_to_composed_groups(self.get_option('groups'), host, hostname)

            # Create groups based on variable values and add the corresponding hosts to it
            self._add_host_to_keyed_groups(self.get_option('keyed_groups'), host, hostname)

    def verify_file(self, path):
        """ return true/false if this is possibly a valid file for this plugin to consume """
        valid = False
        if super(InventoryModule, self).verify_file(path):
            # base class verifies that file exists and is readable by current user
            if path.endswith(('cons3rt.yaml', 'cons3rt.yml')):
                valid = True
        return valid

    def _set_credentials(self, loader):
        """
        :param loader: contents of the inventory config file
        """

        t = Templar(loader=loader)
        credentials = {}

        for credential_type in ['cert_file_path', 'cert_password', 'cons3rt_token']:
            if t.is_template(self.get_option(credential_type)):
                credentials[credential_type] = t.template(variable=self.get_option(credential_type),
                                                          disable_lookups=False)
            else:
                credentials[credential_type] = self.get_option(credential_type)

        self.cert_file_path = credentials['cert_file_path']
        self.cert_password = credentials['cert_password']
        self.cons3rt_token = credentials['cons3rt_token']

        if not (self.cert_file_path and self.cert_password and self.cons3rt_token):
            raise AnsibleError("Insufficient credentials found. Please provide them in your "
                               "inventory configuration file.")

    def _get_credentials(self):
        """
        :return: a dictionary of CONS3RT client credentials
        """
        cons3rt_params = {}
        for credential in (('cert_file_path', self.cert_file_path),
                           ('cert_password', self.cert_password),
                           ('cons3rt_token', self.cons3rt_token)):
            if credential[1]:
                cons3rt_params[credential[0]] = credential[1]

        return cons3rt_params

    def parse(self, inventory, loader, path, cache=True):

        # call base method to ensure properties are available for use with other helper methods
        super(InventoryModule, self).parse(inventory, loader, path, cache)

        self._read_config_data(path)
        self._set_credentials(loader)
        self.cons3rt_url = self.get_option('cons3rt_url')

        cache_key = self.get_cache_key(path)
        # false when refresh_cache or --flush-cache is used
        if cache:
            # get the user-specified directive
            cache = self.get_option('cache')

        # Generate inventory
        results = {}
        cache_needs_update = False
        if cache:
            try:
                results = self._cache[cache_key]
            except KeyError:
                # if cache expires or cache file doesn't exist
                cache_needs_update = True

        if not cache or cache_needs_update:
            results = self._query()

        self._populate(results)

        # If the cache has expired/doesn't exist or if refresh_inventory/flush cache is used
        # when the user is using caching, update the cached inventory
        if cache_needs_update or (not cache and self.get_option('cache')):
            self._cache[cache_key] = results
