#!/usr/bin/env python3
import sys
import os
import requests
import yaml
import json
from random import randint, uniform
from time import sleep, time_ns, time
import argparse
import logging
from urllib import parse
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import ASYNCHRONOUS
from aiohttp import web
import asyncio
import threading
from uuid import UUID


def blur_price(s):
    return int(s)+randint(-5, 5)*20


def random_sleep(min_duration, max_duration):
    sleep(uniform(min_duration, max_duration))


def jsonize(text):
    if not text:
        return ''

    try:
        return json.loads(text)
    except json.decoder.JSONDecodeError:
        return text


def itemdata2tags(itemdata):
    tag_keys = [
        'rating',
        'itemType',
        'resourceId',
        'cardsubtypeid',
        'preferredPosition',
        'rareflag',
        'playStyle',
        'leagueId',
        'nation',
        'attributeArray',
        'skillmoves',
        'weakfootabilitytypecode',
        'attackingworkrate',
        'defensiveworkrate',
        'preferredfoot',
    ]
    tags = {}
    for k in itemdata:
        if k in tag_keys:
            tags[k] = str(itemdata[k])

    return tags


def move_maxb(maxb, multiplier=1.01, delta=100):
    new_maxb = maxb*multiplier // 100 * 100
    return new_maxb if abs(new_maxb - maxb) > abs(delta) else maxb + delta


def is_valid_uuid(uuid_to_test, version=4):
    """
    Check if uuid_to_test is a valid UUID.

    Parameters
    ----------
    uuid_to_test : str
    version : {1, 2, 3, 4}

    Returns
    -------
    `True` if uuid_to_test is a valid UUID, otherwise `False`.

    Examples
    --------
    >>> is_valid_uuid('c9bf9e57-1685-4c89-bafb-ff5af830be8a')
    True
    >>> is_valid_uuid('c9bf9e58')
    False
    """
    try:
        uuid_obj = UUID(uuid_to_test, version=version)
    except ValueError:
        return False

    return str(uuid_obj) == uuid_to_test


class SessionException(Exception):
    """Exception Session"""
    pass


class FifaWeb(object):

    def __init__(self, config_file):
        # Load Config
        self.loop = None
        self.app = None
        self.Not200_time = 0
        self.FailRequestInterval = 30  # in seconds
        with open(os.path.expanduser(config_file)) as f:
            self.cfg = yaml.safe_load(f)

        # define some constants
        self.purchased_count = 0
        self.SID_NAME = 'X-UT-SID'
        self.cfg['urls'] = {
            'market':             self.cfg['base_url'] + 'transfermarket',
            'bid':                self.cfg['base_url'] + 'trade/{}/bid',
            'purchased_items':    self.cfg['base_url'] + 'purchased/items',
            'item':               self.cfg['base_url'] + 'item',
            'auction':            self.cfg['base_url'] + 'auctionhouse',
        }

        # Logger Settings
        self.logger = logging.getLogger("fifa_log")
        self.logger.setLevel(logging.INFO)
        if 'logfile' in self.cfg:
            fh = logging.FileHandler(self.cfg['logfile'])
        else:
            fh = logging.StreamHandler()

        fh.setFormatter(logging.Formatter(
            '{"time": "%(asctime)s", "name": "%(name)s", \
                    "level": "%(levelname)s", "message": %(message)s }'
        ))
        self.logger.addHandler(fh)

        # Influx Config
        if 'influxdb' in self.cfg:
            self.influxdb = InfluxDBClient(
                url=self.cfg['influxdb']['url'],
                token=self.cfg['influxdb']['token'],
                org=self.cfg['influxdb']['org']
            )
            self.influx_write_client = self.influxdb.write_api(
                write_options=ASYNCHRONOUS)

        # Create requests session
        self.requests = requests.Session()
        self.requests.headers.update(self.cfg['headers'])

    def load_items(self, filename):
        self.Items = []
        with open(os.path.expanduser(filename)) as f:
            self.Items = yaml.safe_load(f)

    def log_request(self, r, level='debug'):
        if level not in ['info', 'debug']:
            return

        logdata = {
            'action': r.request.method,
            'url': r.request.url,
            'uri': r.request.url.split('?')[0],
            'args': dict(parse.parse_qsl(parse.urlsplit(r.request.url).query)),
            'headers': dict(r.headers),
            'body': jsonize(r.request.body),
            'response': {
                'status': r.status_code,
                'headers':    dict(r.headers),
                'data': jsonize(r.text),
            }
        }

        self.log(logdata, level=level)

    def update_headers(self):
        while not self.app or \
                self.SID_NAME not in self.app['headers']:
            self.log('wait first information from browser')
            sleep(1)

        while self.app['headers'][self.SID_NAME] == self.requests.headers[self.SID_NAME]:
            self.log('wait new headers from plugin')
            sleep(1)

        for h in self.requests.headers:
            self.log(self.app['headers'])
            if h in self.app['headers']:
                self.requests.headers[h] = self.app['headers'][h]

    def validate_request(self):
        logdata = {
            'headers': dict(self.requests.headers),
            'SID_NAME': self.SID_NAME,
        }
        self.log(logdata, level='debug')
        try:
            self.requests.headers[self.SID_NAME]
        except (KeyError, AttributeError):
            return False

        if time() - self.Not200_time < self.FailRequestInterval:
            return False

        return is_valid_uuid(self.requests.headers[self.SID_NAME])

    def exit(self):
        sys.exit(1)

    def get(self, url, params={}):
        if not self.validate_request():
            raise SessionException('get error')

        r = self.requests.get(url, params=params)
        self.log_request(r)
        # 401 - auth require
        # 458 - verification require
        # 512 - temporary blocked ( 1-12 hours )
        if r.status_code != 200:
            self.Not200_time = time()
            self.log_request(r, level='info')
            self.update_headers()
        return r

    def put(self, url, json):
        if not self.validate_request():
            raise SessionException('put error')

        r = self.requests.put(url, json=json)
        self.log_request(r)
        return r

    def post(self, url, json):
        if not self.validate_request():
            raise SessionException('post error')

        r = self.requests.post(url, json=json)
        self.log_request(r)
        return r

    def log(self, message, level='info'):
        try:
            func = getattr(self.logger, level)
        except AttributeError:
            func = getattr(self.logger, 'info')

        return func(json.dumps(message))

    def search(self, params):
        payload = self.cfg['params'].copy()
        payload.update(params)
        r = self.get(self.cfg['urls']['market'], params=payload)

        try:
            return r.json()['auctionInfo']
        except KeyError:
            pass

        return {}    # retrun empty dict if something wrong

    def SearchByIndex(self, index, page=0, maxb=None):
        try:
            params = self.Items[index]['params'].copy()

            # overwrite maxb if defined for dump Dump function fix
            if maxb:
                params['maxb'] = maxb

            # randomize maxb and minb for cache miss hack
            for b in ['minb', 'maxb']:
                if b in params:
                    params[b] = blur_price(params[b])
            params['start'] = self.cfg['market_page_size']*page
            return self.search(params)
        except (IndexError, KeyError):
            return {}

    def ItemSuited(self, index, item):
        prices = self.GetPrices(item)  # extra loop by Items (

        if 'rareonly' in self.Items[index] and \
                self.Items[index]['rareonly'] and \
                not item['itemData']['rareflag']:
            return False

        if item['itemData']['itemType'] == 'player' and \
                item['itemData']['rating'] >= self.Items[index]['rating'] and \
                item['buyNowPrice'] <= prices['bid_limit']:
            return True
        elif item['itemData']['itemType'] == 'training' and \
                item['itemData']['cardsubtypeid'] in [220, 107, 108, 268, 266, 262]:
            return True

        return False

    def Bid(self, tradeId, bid):
        r = self.put(
            self.cfg['urls']['bid'].format(tradeId),
            json={'bid': bid},
        )

        if r.status_code != 200:
            return False

        if self.bid_limit:
            self.bid_limit -= 1

        self.log({'bid_limit': self.bid_limit, })
        return True

    def SaveItem(self, item):
        if self.influx_write_client:
            self.influx_write_client.write(self.cfg['influxdb']['bucket'], self.cfg['influxdb']['org'],
                                           {"measurement": "items", "tags": itemdata2tags(item['itemData']),
                                            "fields": {"buynow":  item['buyNowPrice']},
                                            "time": time_ns(), }
                                           )

    def DumpItemByIndex(self, index, maxb=None):
        if not maxb:
            maxb = self.Items[index]['params']['maxb']

        for page in range(self.cfg['market_page_limit']):
            items = self.SearchByIndex(index, page=page, maxb=maxb)
            random_sleep(0.5, 1.5)
            if not items and page == 0:
                maxb = move_maxb(maxb, 1.05, delta=100)
                break

            for item in items:
                self.SaveItem(item)
                # self.log(item)

            if len(items) <= self.cfg['market_page_size']:  # last not empty page
                break

            if page == self.cfg['market_page_limit'] - 1:  # all 3 pages were full
                maxb = move_maxb(maxb, 0.97, delta=-100)

        return maxb

    def BuyItemByIndex(self, index):
        for page in range(self.cfg['market_page_limit']):
            items = self.SearchByIndex(index, page=page)
            for item in items:
                self.SaveItem(item)
                if self.ItemSuited(index, item):
                    self.log(item)
                    self.Bid(item['tradeId'], item['buyNowPrice'])
                    self.purchased_count += 1

            if len(items) < self.cfg['market_page_size']:  # last page
                break

            random_sleep(1, 4)

    def BuyRandomItem(self):
        self.BuyItemByIndex(randint(0, len(self.Items)-1))

    def GetPurchasedItems(self):
        try:
            return self.get(self.cfg['urls']['purchased_items']).json()['itemData']
        except KeyError:
            return []

    def GetItemByResourseId(self, resourceId):
        default_item = {}
        for item in self.Items:
            if item['resourceId'] == resourceId:
                return item
            elif item['resourceId'] == 0:
                default_item = item

        return default_item

    def GetPrices(self, item):  # gets item and purchased_item as argument
        try:
            item_data = item['itemData']
        except KeyError:
            item_data = item

        prices = self.GetItemByResourseId(item_data['resourceId'])['prices']

        # set rare prices if defined
        if item_data['rareflag'] and 'rare' in prices:
            try:
                prices = prices['rare']
            except KeyError:
                pass

        return prices

    def MoveToTradePill(self, item):
        r = self.put(self.cfg['urls']['item'],
                     json={'itemData': [{'id': item['id'],
                                         'pile': 'trade',
                                         }, ],
                           },
                     )

        if r.status_code != 200:
            return False

        return True

    def Auction(self, item):
        prices = self.GetPrices(item)
        r = self.post(
            self.cfg['urls']['auction'],
            json={'itemData':
                  {'id': item['id'], },
                  'startingBid': prices['start'],
                  'duration': 3600,
                  'buyNowPrice': prices['buynow'],
                  },
        )

        if r.status_code != 200:
            return False

        self.log({
            'item': item,
            'purchase': prices['buynow'],
        })
        return True

    def ItemCanBeSold(self, purchased_item):
        prices = self.GetPrices(purchased_item)

        # if prices exist and greather than 0
        if ('start' in prices and 'buynow' in prices
                and int(prices['start']) > 0 and int(prices['buynow']) > 0):
            return True

        return False

    def SellPurchasedItems(self):
        for purchased_item in self.GetPurchasedItems():
            random_sleep(1, 2)
            if self.ItemCanBeSold(purchased_item):
                if self.MoveToTradePill(purchased_item):
                    random_sleep(0.4, 1)
                    self.Auction(purchased_item)
                    random_sleep(5, 10)
        self.purchased_count = 0

    def DecodeSearchUrl(self, url):
        r = self.get(url)
        try:
            first = r.json()['auctionInfo'][0]
        except (KeyError, IndexError, ):
            self.log('Try one more time, or increase maxb')
            self.exit()

        # print(json.dumps(first))
        item_tmpl = [{
            'name': 'OptionalValue',
            'resourceId': first['itemData']['resourceId'],
            'params': dict(parse.parse_qsl(parse.urlsplit(r.request.url).query)),
            'prices': {
                'start': first['startingBid'],
                'buynow': first['buyNowPrice'],
            },
        }]

        if first['itemData']['itemType'] == 'player':
            # item_tmpl[0]['desc'] = 'OptionalValue'
            item_tmpl[0]['rating'] = 'MandatoryValue'
            item_tmpl[0]['prices']['bid_limit'] = first['startingBid']

        print(yaml.dump(item_tmpl))

    def aiohttp_server(self):
        def http_get(request):
            self.app['headers'].update(dict(request.headers))
            return web.Response(text='OK')

        self.app = web.Application()
        self.app['headers'] = {}
        self.app.add_routes([web.get('/', http_get)])
        self.runner = web.AppRunner(self.app)
        return self.runner

    def run_server(self, runner):
        if 'web_port' not in self.cfg or not self.cfg['web_port']:
            return

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, 'localhost', 8080)
        self.loop.run_until_complete(site.start())
        self.loop.run_forever()

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


def main():
    parser = argparse.ArgumentParser(description='Fifa Config Parser')
    parser.add_argument('-c', '--config', type=str, default='fifa20.yaml',
                        help='config yaml file')
    parser.add_argument('-i', '--items', type=str, help='items yaml file')
    parser.add_argument('--decode-url', type=str, default='',
                        help='decode search url copied from browser debug console')
    parser.add_argument('--buy-now', type=int, default=0,
                        help='Only price for all purchased itms')
    parser.add_argument('--tries', type=int, default=sys.maxsize,
                        help='how many times we will try to buy an item')
    parser.add_argument('--bid-limit', type=int, default=1,
                        help='how many items we will buy')
    parser.add_argument('--dump', dest='dump', action='store_true')
    parser.add_argument('--web', dest='web', action='store_true')
    parser.add_argument('--buy', dest='buy', action='store_true')
    parser.add_argument('--sell', dest='sell', action='store_true')
    parser.add_argument('-v', '--verbose', dest='debug', action='store_true')
    parser.set_defaults(buy=False)
    parser.set_defaults(sell=False)
    parser.set_defaults(decode_url=False)
    parser.set_defaults(debug=False)
    args = parser.parse_args()

    fifa = FifaWeb(args.config)
    fifa.load_items(args.items)
    fifa.bid_limit = args.bid_limit

    if args.debug:
        fifa.logger.setLevel(logging.DEBUG)

    if args.web:
        t = threading.Thread(target=fifa.run_server, args=(fifa.aiohttp_server(),))
        t.start()

        # wait auth update to prevent auth fail ban
        fifa.update_headers()

    if args.dump:
        maxb = 0  # set default value from item yaml
        for i in range(args.tries):
            # save maxb from preview search
            maxb = fifa.DumpItemByIndex(0, maxb)
            fifa.log('next price {}'.format(maxb))
            random_sleep(5, 15)

    if args.buy:
        for i in range(args.tries):
            fifa.BuyRandomItem()
            random_sleep(3, 9)
            if args.sell and fifa.purchased_count and not i % 20:
                fifa.SellPurchasedItems()
            if fifa.bid_limit <= 0:
                break

    if args.sell:
        fifa.SellPurchasedItems()

    if args.decode_url:
        fifa.DecodeSearchUrl(args.decode_url)

    fifa.stop()


if __name__ == '__main__':
    main()
