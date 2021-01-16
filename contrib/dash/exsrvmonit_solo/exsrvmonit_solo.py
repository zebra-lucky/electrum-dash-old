#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import aiorpcx
import asyncio
import errno
import encodings.idna  # noqa: F401 (need for pyinstaller build)
import fcntl
import json
import os
import socket
import smtplib
import ssl
import stat
import sys
import time
import certifi
import aiohttp
from aiorpcx import RPCSession, SOCKSProxy, NetAddress
from aiorpcx.rawsocket import RSClient
from aiohttp_socks import ProxyConnector, ProxyType
from email.message import EmailMessage


CLIENT_NAME = 'exsrvmonit'
HOME_DIR = os.path.expanduser('~')
DATA_DIR = os.path.join(HOME_DIR, f'.{CLIENT_NAME}')
PID = str(os.getpid())
MIN_PROTO_VERSION = '1.4.2'
NUM_RECENT_DATA = 1440


def get_ssl_context():
    sslc = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    sslc.check_hostname = False
    sslc.verify_mode = ssl.CERT_NONE
    return sslc


def read_recent_file(recent_fname):
    if not os.path.isfile(recent_fname):
        return []
    with open(recent_fname, 'r', encoding='utf-8') as f:
        data = f.read()
        return json.loads(data)


def save_recent_file(recent_data, recent_fname):
    s = json.dumps(recent_data, indent=4, sort_keys=True)
    with open(recent_fname, 'w', encoding='utf-8') as f:
        f.write(s)


def add_to_recent_file(is_working, recent_fname):
    recent_data = read_recent_file(recent_fname)
    recent_data.insert(0, {'time': time.time(), 'is_working': is_working})
    recent_data = recent_data[:NUM_RECENT_DATA]
    save_recent_file(recent_data, recent_fname)
    return recent_data


def list_recent_file(recent_fname):
    recent_data = reversed(read_recent_file(recent_fname))
    for r in recent_data:
        check_time = time.ctime(r['time'])
        is_working = r['is_working']
        print(f'{check_time}: is working: {is_working}')


async def gather_info(host, port, use_tor):
    try:
        sslc = get_ssl_context()
        res = {'server': f'{host}:{port}'}
        if args.force_tor or host.endswith('.onion'):
            PROXY_ADDR = aiorpcx.NetAddress(args.tor_host, args.tor_port)
            PROXY = SOCKSProxy(PROXY_ADDR, aiorpcx.socks.SOCKS5, None)
        else:
            PROXY = None
        async with RSClient(session_factory=RPCSession, host=host, port=port,
                            ssl=sslc, proxy=PROXY) as session:
            session.sent_request_timeout = session.max_send_delay = 45
            ver = await session.send_request('server.version',
                                             [CLIENT_NAME, MIN_PROTO_VERSION])
            tip = await session.send_request('blockchain.headers.subscribe')
            res.update({'version': ver[0],
                        'proto': ver[1],
                        'height': tip['height']})
            return res
    except aiorpcx.jsonrpc.RPCError:
        return res
    except (ConnectionError, TimeoutError):
        return res
    except aiorpcx.socks.SOCKSFailure:
        return res
    except socket.error as e:
        if e.errno == errno.ECONNREFUSED:
            return res
        raise


def check_server_is_working_for_period(recent_data):
    recent_len = len(recent_data)
    num_fails = args.num_fails
    server = args.server
    if not recent_data[0]['is_working']:
        if recent_len < num_fails:
            return
        for i in range(1, num_fails):
            if recent_data[i]['is_working']:
                return
        alert_msg_subj = f'{server} is not working'
        alert_msg_body = (f'In the last {num_fails} checks {server}'
                          f' is not working.')
        return (alert_msg_subj, alert_msg_body)
    else:
        if recent_len < num_fails + 1:
            return
        for i in range(1, num_fails+1):
            if recent_data[i]['is_working']:
                return
        alert_msg_subj = f'{server} is working'
        alert_msg_body = f'{server} is working again.'
        return (alert_msg_subj, alert_msg_body)


def send_email(subj, body, email_to):
    msg = EmailMessage()
    msg['Subject'] = subj
    msg['From'] = CLIENT_NAME
    msg['To'] = email_to
    msg.set_content(body)
    s = smtplib.SMTP('localhost')
    s.send_message(msg)
    s.quit()


def check_recent_and_alert(recent_data, check_fn):
    alert_data = check_fn(recent_data)
    if not alert_data:
        return

    subj, body = alert_data

    if args.notify_cron:
        print(f'{subj}:\n{body}')

    if args.email_to:
        email_subj = f'[{CLIENT_NAME.capitalize()}] {subj}'
        for email_to in args.email_to:
            send_email(email_subj, body, email_to)


def make_aiohttp_session(use_tor):
    ssl_context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH,
                                             cafile=certifi.where())
    headers = {'User-Agent': CLIENT_NAME}
    timeout = aiohttp.ClientTimeout(total=45)
    if use_tor:
        connector = ProxyConnector(
            proxy_type=ProxyType.SOCKS5,
            host=args.tor_host,
            port=args.tor_port,
            username=None,
            password=None,
            rdns=True,
            ssl=ssl_context,
        )
    else:
        connector = aiohttp.TCPConnector(ssl=ssl_context)
    return aiohttp.ClientSession(headers=headers, timeout=timeout,
                                 connector=connector)


async def get_height(use_tor):
    if args.testnet:
        uri = 'https://testnet-insight.dashevo.org/insight-api/blocks?limit=1'
    else:
        uri = 'https://insight.dash.org/api/blocks?limit=1'
    error_msg = f'can not get height from {uri}'
    async with make_aiohttp_session(use_tor) as session:
        async with session.get(uri) as response:
            if response.status != 200:
                print(error_msg)
                return 0
            res_json = await response.json()
            blocks = res_json.get('blocks', [])
            if not blocks or type(blocks) != list:
                print(error_msg)
                return 0
            block = blocks[0]
            if not block or type(block) != dict:
                print(error_msg)
                return 0
            return block.get('height', 0)


async def main(server, recent_fname):
    server = args.server
    host, port = server.split(':')
    use_tor = args.force_tor or host.endswith('.onion')
    height = await get_height(use_tor)
    res = await gather_info(host, port, use_tor)
    is_working = False
    if 'version' in res and 'height' in res:
        if height - res['height'] < 10:
            is_working = True
    recent_data = add_to_recent_file(is_working, recent_fname)
    check_recent_and_alert(recent_data, check_server_is_working_for_period)


def run_exclusive():
    if os.path.exists(DATA_DIR):
        if not os.path.isdir(DATA_DIR):
            print(f'{DATA_DIR} is not directory')
            sys.exit(1)
    else:
        os.mkdir(DATA_DIR)
        os.chmod(DATA_DIR, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    server = args.server
    server_fname = server.replace(':', '_')
    pid_fname = os.path.join(DATA_DIR, '%s.pid' % server_fname)
    recent_fname = os.path.join(DATA_DIR, '%s.recent_data' % server_fname)
    if args.list_recent:
        list_recent_file(recent_fname)
        sys.exit(0)
    if args.test_emails:
        if not args.email_to:
            print(f'can not send test emails, no emails specified')
            sys.exit(1)
        for email_to in args.email_to:
            send_email(f'[{CLIENT_NAME.capitalize()}] test for {args.server}',
                       f'test email from {CLIENT_NAME} for {args.server}',
                       email_to)
        sys.exit(0)
    try:
        ex_lock = False
        with open(pid_fname, 'wb', 0) as fp:
            fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            ex_lock = True
            fp.write(f'{PID}\n'.encode())
            loop = asyncio.get_event_loop()
            loop.run_until_complete(main(server, recent_fname))
    except OSError as e:
        if e.errno in [errno.EACCES, errno.EAGAIN]:
            print(f'can not get exclusive access to {pid_fname}')
            sys.exit(1)
        else:
            raise
    finally:
        if ex_lock:
            os.unlink(pid_fname)


parser = argparse.ArgumentParser()
parser.add_argument('-c', '--notify-cron', default=False, action='store_true',
                    help='Notify cron by msg to console instead sending email')
parser.add_argument('-e', '--email-to', nargs='+', default=[],
                    help='List of emails to send alerts', metavar='EMAIL')
parser.add_argument('-l', '--list-recent', default=False, action='store_true',
                    help='List recent checks results')
parser.add_argument('-m', '--test-emails', default=False, action='store_true',
                    help='Send test emails')
parser.add_argument('-n', '--num-fails', type=int, default=2, metavar='COUNT',
                    help='Number of sequential fails of check to alert')
parser.add_argument('-s', '--server', type=str, required=True,
                    help='Server to check', metavar='SERVER')
parser.add_argument('-t', '--testnet', default=False, action='store_true',
                    help='Use testnet blockchain')
parser.add_argument('--tor-port', type=int, default=9050, metavar='TOR_PORT',
                    help='port of Tor socks proxy')
parser.add_argument('--tor-host', type=str, default='localhost',
                    help='host of Tor socks proxy', metavar='TOR_HOST')
parser.add_argument('--force-tor', default=False, action='store_true',
                    help='Use Tor socks proxy for all servers, not only onion')
args = parser.parse_args()
run_exclusive()
