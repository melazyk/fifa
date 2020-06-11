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


def blur_price(s, r_min=-3, r_max=3, delta=50, min_price=200):
    value = int(s)+randint(r_min, r_max)*delta
    return value if value > min_price else min_price


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


def auction_info_items(r):
    try:
        return r.json()['auctionInfo']
    except (KeyError, json.decoder.JSONDecodeError):
        pass

    return []


def pure_item(item):
    try:
        return item['itemData']
    except KeyError:
        pass

    return item


class SessionException(Exception):
    """ Exception Session """
    pass


class FifaWeb(object):

    def __init__(self, config_file):
        # Load Config
        self.loop = None
        self.app = None
        self.EmptyCount = 0
        self.FailRequestInterval = 30  # in seconds
        self.credits = 0
        self.futbin = False
        self.futcards = False
        self.bid_limit = 1
        self.prices_cache = {}
        with open(os.path.expanduser(config_file)) as f:
            self.cfg = yaml.safe_load(f)

        # define some constants
        self.purchased_count = 0
        self.SID_NAME = 'X-UT-SID'
        self.invalid_sid = ''
        self.cfg['club_page_size'] = 91
        self.club_params = {
            'sort': 'desc',
            'sortBy': 'value',
            'type': 'player',
            'count': self.cfg['club_page_size'],
            # 'start': 91,
        }
        # https://utas.external.s2.fut.ea.com/ut/game/fifa20/user/creditshttps://utas.external.s2.fut.ea.com/ut/game/fifa20/user/credits
        # https://utas.external.s2.fut.ea.com/ut/game/fifa20/club?sort=desc&sortBy=value&type=player&start=91&count=91
        self.cfg['urls'] = {
            'market':             self.cfg['base_url'] + 'transfermarket',
            'bid':                self.cfg['base_url'] + 'trade/{}/bid',
            'purchased_items':    self.cfg['base_url'] + 'purchased/items',
            'item':               self.cfg['base_url'] + 'item',
            'auction':            self.cfg['base_url'] + 'auctionhouse',
            'credits':            self.cfg['base_url'] + 'user/credits',
            'club':               self.cfg['base_url'] + 'club',
            'tradepile':          self.cfg['base_url'] + 'tradepile',
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

        # Validate request
        self.AuthError = self.valid_request()

    def __getattribute__(self, attr):
        """ Log method name in debug mode """
        method = object.__getattribute__(self, attr)
        # if not method:
        #     raise Exception("Method %s not implemented" % attr)
        if not callable(method):
            return method

        if attr != 'log':
            self.log('method {} called'.format(attr), level='debug')

        if attr in ('get', 'put', 'post'):
            if self.AuthError:
                self.update_headers()
            elif not self.valid_request():
                raise SessionException('method {} error'.format(attr))

        return method

    def load_items(self, filename):
        self.Items = []
        with open(os.path.expanduser(filename)) as f:
            self.Items = yaml.safe_load(f)

        # Define Default Arrays
        for item in self.Items:
            # any excludePositions
            item['excludePositions'] = item['excludePositions'] if 'excludePositions' in item else []
            item['rating'] = item['rating'] if 'rating' in item else 0  # any rating
            # any resourceId
            item['resourceId'] = item['resourceId'] if 'resourceId' in item else 0

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

    def get_headers_from_app(self):
        for h in self.requests.headers:
            self.log(self.app['headers'], level='debug')
            if h in self.app['headers']:
                self.requests.headers[h] = self.app['headers'][h]

    def update_headers(self):
        while not self.app or \
                self.SID_NAME not in self.app['headers'] or \
                self.app['headers'][self.SID_NAME] == self.invalid_sid:

            try:
                self.log({
                    'text': 'wait new headers from plugin',
                    'old_sid': self.requests.headers[self.SID_NAME],
                    'new_sid': self.app['headers'][self.SID_NAME],
                })
            except (KeyError, AttributeError):
                self.log({
                    'text': 'wait first headers from plugin',
                })
            sleep(1)

        self.get_headers_from_app()

    def valid_request(self):
        logdata = {
            'headers': dict(self.requests.headers),
            'now': time(),
            'FailRequestInterval': self.FailRequestInterval,
            'SID_NAME': self.SID_NAME,
        }

        self.log(logdata, level='debug')
        try:
            self.requests.headers[self.SID_NAME]
        except (KeyError, AttributeError):
            return False

        return is_valid_uuid(self.requests.headers[self.SID_NAME])

    def response_handler(self, r):
        # ConnectionResetError
        # https://github.com/futapi/fut/blob/master/fut/exceptions.py
        # 200 - OK
        # 401 - auth require
        # 403 - forbidden
        # 409 - conflict
        # 512 - temporary block ( 1-12 hours )
        # 426 - upgrade require ( I think it isn't a real upgrade cononection )
        # 458 - verification require
        # 459 - capthca triggered
        # 461 - permission denied ( You are not allowed to bid on this trade )
        # 478 - NO_TRADE_EXISTS
        # 482 - invalide cookie
        # 494 - new account transfer market locked
        if r.status_code in [401, 403, 458, 426, 459]:
            self.log_request(r, level='info')
            self.AuthError = True
            # let's save invalid SID
            self.invalid_sid = self.requests.headers[self.SID_NAME]
        elif r.status_code in [409, ]:
            self.log_request(r, level='info')
        elif r.status_code in [200, 461, 478, ]:
            self.AuthError = False
            self.log_request(r)
        else:
            self.log_request(r, level='info')
            raise SessionException('UT API Error')

    def get(self, url, params={}):
        r = self.requests.get(url, params=params)
        self.response_handler(r)
        return r

    def put(self, url, json):
        r = self.requests.put(url, json=json)
        self.response_handler(r)
        return r

    def post(self, url, json):
        r = self.requests.post(url, json=json)
        self.response_handler(r)
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
        return auction_info_items(r)

    def tradepile(self):
        r = self.get(self.cfg['urls']['tradepile'])
        return auction_info_items(r)

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

    def club(self, params={'start': 0, }):
        payload = self.club_params.copy()
        payload.update(params)
        r = self.get(self.cfg['urls']['club'], params=payload)

        try:
            return r.json()['itemData']
        except (KeyError, json.decoder.JSONDecodeError):
            pass

        return []  # retrun empty dict if something wrong

    def GetClubPlayers(self):
        players = []
        page = 0

        while True:
            p = self.club(params={'start': page*self.cfg['club_page_size']})
            players += p
            page += 1

            if len(p) < self.cfg['club_page_size']:
                return players

        return players

    def ItemSuited(self, index, item):
        if 'rareonly' in self.Items[index] and \
                self.Items[index]['rareonly'] and \
                not item['itemData']['rareflag']:
            return False

        if item['itemData']['itemType'] == 'player':
            if item['itemData']['rating'] < self.Items[index]['rating']:
                return False

            if item['itemData']['preferredPosition'] in self.Items[index]['excludePositions']:
                return False

            if 'profit' in self.Items[index]:
                profit = self.GetExternalPrice(item['itemData']['resourceId'])*0.95 - item['buyNowPrice']
                self.log({'potential_profit': profit, 'credits': self.credits})
                if self.Items[index]['profit'] <= profit:
                    return True
                else:
                    return False
            elif item['buyNowPrice'] <= self.Items[index]['params']['maxb']:
                return True

        if item['itemData']['itemType'] == 'training' and item['itemData']['cardsubtypeid'] in [220, 107, 108, 268, 266, 262]:
            return True

        return False

    def set_credits(self, credits):
        """ dumb function for budget calculatein in future """
        self.credits = int(credits)

    def Bid(self, tradeId, bid):
        r = self.put(
            self.cfg['urls']['bid'].format(tradeId),
            json={'bid': bid},
        )

        try:
            self.set_credits(r.json()['credits'])
        except (KeyError, json.decoder.JSONDecodeError):
            pass

        if r.status_code != 200:
            return False

        if self.bid_limit:
            self.bid_limit -= 1

        self.log({'bid_limit': self.bid_limit, 'credits': self.credits})
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
                    # sleep over 1s
                    random_sleep(1.5, 4)
                if self.bid_limit <= 0:
                    return

            if len(items) < self.cfg['market_page_size']:  # last page
                break

            random_sleep(1, 4)

    def BuyRandomItem(self):
        self.BuyItemByIndex(randint(0, len(self.Items)-1))

    def GetItemByResourseId(self, resourceId):
        default_item = {}
        for item in self.Items:
            if item['resourceId'] == resourceId:
                return item
            elif item['resourceId'] == 0:
                default_item = item

        return default_item

    def GetFutbinPrice(self, resourceId, platform='ps', price_type='LCPrice2'):
        # gets item and purchased_item Das argument
        # platforms: ps, pc, xbox
        #  prcie_types
        # "LCPrice": "6,500",
        # "LCPrice2": "6,600",
        # "LCPrice3": "6,700",
        # "LCPrice4": "7,800",
        # "LCPrice5": 0,
        #  "updated": "1 hour ago",
        # "MinPrice": "300",
        # "MaxPrice": "10,000",
        # "PRP": "63"
        if resourceId in self.prices_cache:
            return self.prices_cache[resourceId]

        try:
            r = requests.get(
                'https://www.futbin.com/20/playerPrices?player={}'.format(resourceId))
            price = int(r.json()[str(resourceId)]['prices']
                        [platform][price_type].replace(',', ''))
            self.prices_cache[resourceId] = price
        except (KeyError, TypeError, json.decoder.JSONDecodeError):
            return 0

        return price

    def GetFutcardsPrice(self, resourceId, platform='ps'):
        if resourceId in self.prices_cache:
            return self.prices_cache[resourceId]

        try:
            r = requests.get(
                    'https://futcards.info/api/cards/price/YuvN42LUeVvJ7npYELPKE8l6JXgbrZJ7/{}'.format(resourceId))
            price = int(r.json()[str(resourceId)]['prices'][platform]['price'])
            self.prices_cache[resourceId] = price
        except (KeyError, TypeError, json.decoder.JSONDecodeError):
            return 0

        return price

    def GetExternalPrice(self, resourceId):
        if self.futbin:
            return self.GetFutbinPrice(resourceId)
        elif self.futcards:
            return self.GetFutcardsPrice(resourceId)

    def GetPrice(self, item_data):
        price = 0
        if self.futbin or self.futcards:
            price = self.GetExternalPrice(item_data['resourceId'])
        else:
            price = self.GetItemByResourseId(item_data['resourceId'])['price']

        return price

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
        item_data = pure_item(item)
        if not self.ItemCanBeSold(item_data):
            return False

        price = self.GetPrice(item_data)
        start = blur_price(price, r_min=-4, r_max=-2, delta=100)
        buynow = price

        if start < item_data['marketDataMinPrice']:
            start = item_data['marketDataMinPrice']

        if buynow > item_data['marketDataMaxPrice']:
            buynow = item_data['marketDataMaxPrice']

        r = self.post(
            self.cfg['urls']['auction'],
            json={'itemData':
                  {'id': item_data['id'], },
                  'startingBid': start,
                  'duration': 3600,
                  'buyNowPrice': buynow,
                  },
        )

        if r.status_code != 200:
            return False

        self.log({
            'item': item_data,
            'purchase': buynow,
        })
        return True

    def ItemCanBeSold(self, item_data):
        if self.GetPrice(item_data) > 0:
            return True
        return False

    def GetPurchasedItems(self):
        try:
            return self.get(self.cfg['urls']['purchased_items']).json()['itemData']
        except KeyError:
            return []

    def MovePurchasedItems(self):
        for item_data in self.GetPurchasedItems():
            random_sleep(3, 8)
            self.MoveToTradePill(item_data)

        self.purchased_count = 0

    def SellFromTradePile(self):
        for item in self.tradepile():
            self.Auction(item)
            random_sleep(3, 8)

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
            'price': first['buyNowPrice'],
        }]

        if first['itemData']['itemType'] == 'player':
            item_tmpl[0]['rating'] = 'OptionalValue'

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
    parser.add_argument('--futbin', dest='futbin', action='store_true')
    parser.add_argument('--futcards', dest='futcards', action='store_true')
    parser.add_argument('--buy', dest='buy', action='store_true')
    parser.add_argument('--sell', dest='sell', action='store_true')
    parser.add_argument('-v', '--verbose', dest='debug', action='store_true')
    parser.set_defaults(buy=False)
    parser.set_defaults(sell=False)
    parser.set_defaults(decode_url=False)
    parser.set_defaults(debug=False)
    args = parser.parse_args()

    fifa = FifaWeb(args.config)
    if args.items:
        fifa.load_items(args.items)
    fifa.bid_limit = args.bid_limit

    if args.debug:
        fifa.logger.setLevel(logging.DEBUG)

    if args.futbin:
        fifa.futbin = args.futbin
    if args.futcards:
        fifa.futcards = args.futcards

    if args.web:
        t = threading.Thread(target=fifa.run_server,
                             args=(fifa.aiohttp_server(),))
        t.start()

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
            # try to sell all items once per 20 searches
            if args.sell and fifa.purchased_count and not fifa.purchased_count % 5:
                fifa.MovePurchasedItems()
                fifa.SellFromTradePile()
            if fifa.bid_limit <= 0:
                break

    if args.sell:
        # fifa.MovePurchasedItems()
        fifa.SellFromTradePile()

    if args.decode_url:
        fifa.DecodeSearchUrl(args.decode_url)

    fifa.stop()


if __name__ == '__main__':
    try:
        main()
    except SessionException:
        os.system('kill %d' % os.getpid())
