from contextlib import contextmanager
import argparse
import itertools
import json
import logging
import os
import subprocess
import sys
import socket
import ssl
import urllib2

import BeautifulSoup
import colorlog
import IPy
import netaddr
import pyasn

local_path = os.path.dirname(os.path.realpath(__file__))
sys.path.append(local_path + '/lib')
from ssdp_functions import recurse_ssdp_check
from ntp_functions import ntp_monlist_check
from dns_functions import recurse_DNS_check


logger = colorlog.getLogger('lift')


def configure_logging(level=logging.DEBUG, write_to_file=False, filename=''):
    '''Configure the logger.

    Args:
        level (str, optional): Sets the severity level of the messages to be
            displayed in the log. Defaults to logging.DEBUG, the lowest level.
        write_to_file (str, optional): Whether to write the log messages to a
            file. Defaults to False.
        filename (str, optional): The name of the file where log messages
            should be written. Defaults to '' since log messages are written to
            the console by default.

    Returns:
        None
    '''
    format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    if write_to_file:
        handler = logging.FileHandler(filename)
        formatter = logging.Formatter(format)
    else:
        handler = colorlog.StreamHandler()
        formatter = colorlog.ColoredFormatter(
            '%(log_color)s' + format,
            datefmt=None,
            reset=True,
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'purple',
            },
            secondary_log_colors={},
            style='%'
        )

    logger.setLevel(level)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class UsageError(Exception):
    '''Exception raised for errors in the usage of this module.

    Attributes:
        msg  -- explanation of the error
    '''
    def __init__(self, msg):
        self.msg = msg


def parse_args():
    '''Parse the command line attributes and return them as the dict `options`.
    '''
    parser = argparse.ArgumentParser(description='Low Impact Identification'
                                     ' Tool')
    argroup = parser.add_mutually_exclusive_group(required=True)
    argroup.add_argument("-i", "--ip", dest='ip', help="An IP address")
    argroup.add_argument("-f", "--ipfile", dest='ipfile', help="A file of IPs")
    argroup.add_argument("-s", "--subnet", dest='subnet', help="A subnet!")
    argroup.add_argument("-a", "--asn", dest='asn', type=int,
                         help="ASN number. WARNING: This will take a while")
    parser.add_argument("-p", "--port", dest='port', type=int, default=443,
                        help=" The port number at the supplied IP address that"
                        " lift should connect to")
    parser.add_argument("-r", "--recurse", dest='recurse', action="store_true",
                        default=False, help="Test Recursion")
    parser.add_argument("-R", "--recon", dest='recon', action="store_true",
                        default=False, help="Gather info about a given device")
    parser.add_argument("-v", "--verbose", dest='verbose', action="store_true",
                        default=False, help="WARNING DO NOT USE -v UNLESS YOU"
                        "WANT ALL THE REASONS WHY SOMETHING IS FAILING.")
    # TODO Is --ssl flag still needed?
    args = parser.parse_args()
    options = vars(args)
    logger.debug('Parsed the cli args: %s' % options)
    return options


def get_ips_from_ip(options):
    '''Return a list with the IP address supplied to the command line.
    '''
    return [options['ip']] if options['ip'] else []


@contextmanager
def opened_w_error(filename, mode="r"):
    '''
    A factory function that allows us to enter and exit the opened file context
    while also catching and yielding any errors that occur in that context.

    Args:
        filename (str): The name of the file to be opened.
        mode (str, optional): String indicating how the file is to be opened.
            Defaults to 'r', reading mode.

    Yields:
        f (file):  The file object.
        err (IOError): If the file cannot be opened.
    '''
    try:
        f = open(filename, mode)
    except IOError, err:
        yield None, err
    else:
        try:
            yield f, None
        finally:
            f.close()


def get_ips_from_file(options):
    '''Read each line of the IP file and return a list of IP addresses.
    '''
    ip_list = []

    with opened_w_error(options['ipfile']) as (f, err):
        if err:
            logger.error(err)
        else:
            ip_list = f.readlines()
            logger.debug("Found %d IPs in the given ipfile: %s" %
                        (len(ip_list), options['ipfile']))
    return ip_list


def get_ips_from_subnet(options):
    '''Return a list of IP addresses in the given subnet.
    '''
    ip_list = []

    try:
        ip_list = [ip for ip in netaddr.IPNetwork(options['subnet'])]
        logger.debug("Found %d IPs in the given subnet: %s" %
                    (len(ip_list), options['subnet']))
    except (netaddr.core.AddrFormatError, ValueError) as err:
        logger.error(err)

    return ip_list


def get_ips_from_asn(options):
    '''Lookup and return a list of IP addresses associated with the
    subnets in the given Autonomous System Number.
    '''
    ip_list = []

    try:
        ipasn_file = local_path + '/lib/ipasn.dat'
        asndb = pyasn.pyasn(ipasn_file)
        subnets = [subnet for subnet in asndb.get_as_prefixes(options['asn'])]
        logger.debug("Found %d prefixes advertised by the given ASN: %s" %
                    (len(subnets), options['asn']))
    except Exception, err:
        logger.error("AsnError: %s" % err)
    else:
        # creates a nested list of lists
        nested_ip_list = [get_ips_from_subnet(subnet) for subnet in subnets]

        # flattens the nested list into a shallow list
        ip_list = itertools.chain.from_iterable(nested_ip_list)

    return ip_list


def convert_input_to_ips(options):
    '''Call the correct function to normalize the command line argument that
    contains the IP addresses, and return a list of IP addresses.
    '''
    try:
        dispatch = {
            'ip': get_ips_from_ip,
            'ipfile': get_ips_from_file,
            'subnet': get_ips_from_subnet,
            'asn': get_ips_from_asn,
        }
        correct_function = next(v for k, v in dispatch.items() if options[k])
        ip_list = correct_function(options)
        return ip_list
    except StopIteration, KeyError:
        raise UsageError('None of the cli arguments contained IP addresses.')


def is_valid_ip(ip):
    '''Try to create an IP object using the given ip.
    Return True if an instance is successfully created, otherwise return False.
    '''
    valid = isinstance(ip, netaddr.ip.IPAddress)
    if valid:
        return valid

    try:
        valid = True if IPy.IP(ip) else False
    except ValueError, TypeError:
        logger.error('%s is not a valid IP address' % ip)

    return valid


def get_certs_from_handshake(options):
    '''Negotiates an SSL connection with the given IP address.

    Args:
        options: Keyword arguments containing the user-supplied, cli inputs.

    Returns:
        der_cert (bytes): The SSL certificate as a DER-encoded blob of bytes.
            Defaults to None.
        pem_cert: The SSL certificate as a PEM-encoded string. Defaults to
            empty string.
        ctx (SSLContext): An SSLContext object with default settings.

    Raises:
        socket.error: If any socket-related errors occur.
        TypeError: If the DER-encoded cert that is provided is neither a string
            nor buffer.
        ValueError: If we attempt to get the given IP's certificate before the
            SSL handshake is done.
    '''
    der_cert = None
    pem_cert = ''
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.set_ciphers('ALL')

    try:
        sock = socket.socket()
        sock.settimeout(5)
        ssl_sock = ssl.wrap_socket(sock, cert_reqs=ssl.CERT_NONE)
        ssl_sock.connect((options['ip'], options['port']))
        logger.debug('Connected to %s:%d' % (options['ip'], options['port']))

        der_cert = ssl_sock.getpeercert(True)
        if not der_cert:
            logger.info('%s did not provide an SSL certificate' % ip)

        logger.info('Received an SSL certificate: %s' % str(der_cert))
        pem_cert = str(ssl.DER_cert_to_PEM_cert(der_cert))
        logger.debug('Converted the cert from the DER to the PEM format')

    except TypeError, err:
        logger.debug('ssl.DER_cert_to_PEM_cert() raises a TypeError if the'
                     'given DER-encoded cert is neither a string nor buffer')
        logger.error(err)
    except ValueError, err:
        logger.debug('The SSL handshake might not have been done yet.'
                     'getpeercert() raises ValueError in that case')
        logger.error(err)
    except socket.error, err:
        logger.error(err)
    finally:
        sock.close()
        logger.debug('Closed socket.')
    return der_cert, pem_cert, ctx


def identify_using_http_response(options):
    '''Calls functions for each of the steps required to identify a device
    based on data in the HTTP response headers or resource.
    1. send request
    2. parse response
    3. lookup response data
    4. print findings
    '''
    headers, html = send_http_request(options)
    title, server = parse_response(html, headers)
    device = lookup_http_data(title, server)
    if device:
        print_findings(options['ip'], device, title=title, server=server)
    else:
        logger.info('No matching title/server was found for IP:  %s' %
                    options['ip'])
        logger.info('Trying rtsp since http request didn\'t ID device.')
        send_rstp_request(options['ip'])
    return device


def send_rstp_request(ip):
    new_ip = str(ip).rstrip('\r\n)')
    bashcommand = ('curl --silent rtsp://' + new_ip +
                   ' -I -m 5| grep Server')
    try:
        proc = subprocess.Popen(['bash', '-c', bashcommand],
                                stdout=subprocess.PIPE)
        output = proc.stdout.read()

    except Exception, err:
        logger.error(err)
    else:
        rtsp_server = str(output).rstrip('\r\n)')
        if 'Dahua' in str(rtsp_server):
            print (str(ip).rstrip('\r\n)') +
                   ": Dahua RTSP Server Detected (RTSP Server)")
    return


def send_http_request(options):
    #TODO add logic to try port 443, then port 80
    options['port'] = 443
    url = "https://%s:%s" % (options['ip'], options['port'])
    ctx = options.get('ctx', None)
    headers = {}
    html = ''
    try:
        response = urllib2.urlopen(url, context=ctx, timeout=10)

    except urllib2.URLError as err:
        if hasattr(err, 'reason'):
            logger.error('Failed to reach a server at %s. Reason: %s' %
                        (url, err.reason))
        elif hasattr(err, 'code'):
            logger.error('The server %s couldn\'t fulfill the request.'
                         ' Error code: %s' % (url, err.code))
    else:
        html = response.read()
        headers = response.info()
        response.close()

    return headers, html


def parse_response(html, headers):
    '''Parse the HTML and headers from the HTTP response and return a dict with
    all extracted data.
    '''
    # TODO figure out relevant exception from parsing to catch/react to
    soup = BeautifulSoup.BeautifulSoup(html)
    title_tag = soup.find('title')
    title = str(title_tag.contents[0]) if title_tag else ''
    server = headers.get('Server') or ''

    return title, server


def print_findings(ip, device, title='', server=''):
    msg = str(ip).rstrip('\r\n)') + ": " + device
    extra_params = {'title': title, 'server': server}
    print msg.format(**extra_params)


def identify_using_ssl_cert(options):
    '''Calls functions that correspond to steps involved in identifying a
    device based on data in the HTTP response headers or resource.
    1. get cert from handshake
    2. lookup cert
    3. print findings
    '''
    device = ''
    der_cert, pem_cert, ctx = get_certs_from_handshake(options)
    if not pem_cert:
        return device

    device = lookup_cert(pem_cert)
    if device:
        logger.debug('Found %s as a match for the cert provided by %s' %
                    (device, options['ip']))
        print_findings(options['ip'], device)
    else:
        logger.info('No matching certs were found for IP %s' % options['ip'])
        # TODO add logic to identify_using_http_response since cert provided
        # no insights into which device this is
    return device


def process_ip(options):
    '''Call the correct function(s) to process the IP address based on the
    port and recurse options passed into the command line..
    '''
    dispatch_by_port = {
        options['port'] == 80: [identify_using_http_response],
        options['port'] != 80 and not options['recurse']: [
            identify_using_ssl_cert],
        options['port'] == 53 and options['recurse']: [recurse_DNS_check],
        options['port'] == 123 and options['recurse']: [ntp_monlist_check],
        options['port'] == 1900 and options['recurse']: [recurse_ssdp_check],
        options['port'] != 80 and options['recurse']: [
            recurse_DNS_check, ntp_monlist_check, recurse_ssdp_check],
    }
    try:
        correct_functions = dispatch_by_port[True]
        logger.debug('Calling the function %s to process IP %s' %
                    (correct_functions, options['ip']))
        [func(options) for func in correct_functions]
        # TODO ^^ fix TypeError: 'function' object is not iterable caused by
        # TODO ^^ fix TypeError: recurse_DNS_check() got an unexpected keyword
        # argument 'subnet'
    except KeyError:
        raise ValueError('Unsure how to handle the given port number (%d) with'
                         ' the other cli arguments' % options['port'])


def setup_cert_collection():
    '''Returns the cert_lookup_dict against which the user-supplied IP
    address will be compared for matching SSL certificates or HTTP
    response data.

    Open all the JSON files in the directory `cert_collection`, and
    concatenate the data to form one massive JSON string.
    Convert this JSON string into a Python object, using json.loads()
    and return this object.
    '''
    json_file = '['
    cert_collection_path = (local_path + '/cert_collection')
    cert_files = os.listdir(cert_collection_path)

    num_files = len(cert_files)

    for x in range(num_files):
        cert_file = cert_collection_path + '/' + cert_files[x]

        with opened_w_error(cert_file) as (f, err):
            if err:
                logger.error(err)
            else:
                file_contents = f.read()
                try:
                    json.loads(file_contents)
                    # TODO check whether the correct fields are present
                    # don't use a file if it's missing ssl_cert_info or
                    # http_response_info arrays or their appropriate fields
                except ValueError:
                    logger.error('File %s has invalid JSON' % cert_file)
                    num_files -= 1
                else:
                    json_file += file_contents + ','

    final = json_file.rstrip(',') + ']'

    global cert_lookup_dict
    cert_lookup_dict = json.loads(final)
    logger.debug('Created cert_lookup_dict using %d JSON files from dir %s' %
                 (num_files, cert_collection_path))

    return


def lookup_cert(pem_cert):
    '''Lookup the given PEM cert in a dictionary containing all the certs in
    the cert_collection directory and return the device description if there's
     a match.
    '''
    keys = [cert_lookup_dict[x]['ssl_cert_info'][y]['PEM_cert']
            for x in range(len(cert_lookup_dict))
            for y in range(len(cert_lookup_dict[x]['ssl_cert_info']))
            ]

    values = [cert_lookup_dict[x]['ssl_cert_info'][y]['display_name']
              for x in range(len(cert_lookup_dict))
              for y in range(len(cert_lookup_dict[x]['ssl_cert_info']))
              ]

    pem_dict = dict(zip(keys, values))
    device = pem_dict.get(pem_cert, '')
    return device


def lookup_http_data(title, server):
    '''Lookup the given title and server in a dictionary containing all the
    HTTP response data in the cert_collection directory. Return the device
    description if there's a match.
    '''
    c = cert_lookup_dict

    server_search_terms = [c[x]['http_response_info'][y]['server_search_text']
                           for x in range(len(c))
                           for y in range(len(c[x]['http_response_info']))
                           ]

    title_search_terms = [c[x]['http_response_info'][y]['title_search_text']
                          for x in range(len(c))
                          for y in range(len(c[x]['http_response_info']))
                          ]

    display_names = [c[x]['http_response_info'][y]['display_name']
                     for x in range(len(c))
                     for y in range(len(c[x]['http_response_info']))
                     ]

    lookup_list = zip(server_search_terms, title_search_terms, display_names)

    device_description = next((n[2] for n in lookup_list
                               if n[0] in server and n[1] in title), '')
    return device_description


def main():
    configure_logging()
    setup_cert_collection()
    options = parse_args()
    results = []

    ip_list = convert_input_to_ips(options)
    for ip in ip_list:
        if is_valid_ip(ip):
            # cast the ip value as a string if it's an instance of IPNetwork.
            options['ip'] = str(ip)
            process_ip(options)
            msg = '%s : success' % ip
        else:
            msg = '%s : fail' % ip

        results.append(msg)
    print results
    return results


if __name__ == '__main__':
    main()
