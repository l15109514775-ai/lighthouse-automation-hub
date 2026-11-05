"""HTTPS with exact host allowlists, public address pinning, TLS and bounded reads."""
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlsplit
from services.hub.errors import HubError


def validate_destination(url, allowed_hosts):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in allowed_hosts or parsed.username or parsed.password
            or parsed.port not in (None,443) or parsed.fragment):
        raise HubError('HTTPS_DESTINATION_NOT_AUTHORIZED', review=True)
    return parsed


class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise HubError('NON_PUBLIC_DESTINATION', review=True)
        # Pin this connection to a validated address; retain the original hostname for TLS/SNI.
        connection = socket.create_connection((addresses[0][4][0], self.port), timeout=self.timeout)
        try:
            # Match HTTPConnection's small-write behavior before TLS wraps the
            # pinned TCP socket; TLS/SNI still uses the authorized hostname.
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = ssl.create_default_context().wrap_socket(connection, server_hostname=self.host)
        except BaseException:
            connection.close()
            raise


def open_request(url, allowed_hosts, *, method='GET', headers=None, body=None, timeout=20):
    if type(timeout) not in (int, float) or not 1 <= timeout <= 900:
        raise HubError('HTTPS_TIMEOUT_INVALID')
    parsed = validate_destination(url, allowed_hosts)
    connection = PublicHTTPSConnection(parsed.hostname, 443, timeout=timeout)
    try:
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return connection, response
    except BaseException:
        connection.close()
        raise


def post_json(url, allowed_hosts, headers, body):
    connection, response = open_request(url, allowed_hosts, method='POST', headers=headers, body=body)
    try:
        data = response.read(262145)
        if len(data) > 262144:
            raise HubError('CALLBACK_RESPONSE_TOO_LARGE')
        return response.status, data
    finally:
        connection.close()
