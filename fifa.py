#!/usr/bin/env python3
import sys
import os
import requests
import yaml
import simplejson
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


def delta_by_price(price):
    steps = (
        (1000, 50),
        (10000, 100),
        (50000, 250),
        (100000, 500),
        (1000000, 1000),
    )

    for step in steps:
        if price < step[0]:
            return step[1]

    return 0


def blur_price(s, ratio=1, min_price=200):
    """
        s         - number wich must be blured
        ratio     - ratio for bluring
        min_price - minimum price

        ratio -1..0 - s*(ratio) .. s
        ratio  0..1 - min_price .. s*ratio
    """
    delta = delta_by_price(s)  # get correct delta
    min_value = min_price
    max_value = s

    if abs(ratio) > 1:
        pass
    elif ratio > 0:
        max_value = s * ratio
    elif ratio < 0:
        min_value = s * abs(ratio)

    # foolproof #1
    if min_value > max_value:
        min_value = max_value

    value = randint(int(min_value), int(max_value + delta))

    # foolproof #2
    if value > s:
        value = s

    value = (value // delta) * delta

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
    new_maxb = maxb * multiplier // 100 * 100
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
        self.quick_sell_ids = []
        self.quick_sell_price = 0
        self.loop = None
        self.app = None
        self.EmptyCount = 0
        self.FailRequestInterval = 30  # in seconds
        self.actual_price_time = 1 * 60 * 60  # in seconds ( 3h )
        self.credits = 0
        self.futbin = False
        self.futcards = True
        self.bid_limit = 1
        self.prices_cache = {}
        self.buy_pack_fails = 0
        with open(os.path.expanduser(config_file)) as f:
            self.cfg = yaml.safe_load(f)

        # define some constants
        self.purchased_count = 0
        self.empty_searches = 0
        self.transfer_closed = False
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
        # https://utas.external.s2.fut.ea.com/ut/game/fifa23/user/credits
        self.cfg['urls'] = {
            'market':
            self.cfg['base_url'] + '/ut/game/fifa23/transfermarket',
            'bid':
            self.cfg['base_url'] + '/ut/game/fifa23/trade/{}/bid',
            'purchased_items':
            self.cfg['base_url'] + '/ut/game/fifa23/purchased/items',
            'item':
            self.cfg['base_url'] + '/ut/game/fifa23/item',
            'auction':
            self.cfg['base_url'] + '/ut/game/fifa23/auctionhouse',
            'credits':
            self.cfg['base_url'] + '/ut/game/fifa23/user/credits',
            'club':
            self.cfg['base_url'] + '/ut/game/fifa23/club',
            'tradepile':
            self.cfg['base_url'] + '/ut/game/fifa23/tradepile',
            'sold':
            self.cfg['base_url'] + '/ut/game/fifa23/trade/sold',
            'delete':
            self.cfg['base_url'] + '/ut/delete/game/fifa23/item',
            'sets':
            self.cfg['base_url'] + '/ut/game/fifa23/sbs/sets',
            'setId':
            self.cfg['base_url'] + '/ut/game/fifa23/sbs/setId/{}/challenges',
            'sbcId':
            self.cfg['base_url'] + '/ut/game/fifa23/sbs/challenge/{}/squad',
        }

        # Logger Settings
        self.logger = logging.getLogger("fifa_log")
        self.logger.setLevel(logging.INFO)
        if 'logfile' in self.cfg:
            fh = logging.FileHandler(self.cfg['logfile'])
        else:
            fh = logging.StreamHandler()

        fh.setFormatter(
            logging.Formatter('{"time": "%(asctime)s", "name": "%(name)s", \
"level": "%(levelname)s", "message": %(message)s }'))
        self.logger.addHandler(fh)

        # Influx Config
        self.influx_write_client = None
        if 'influxdb' in self.cfg:
            self.influxdb = InfluxDBClient(url=self.cfg['influxdb']['url'],
                                           token=self.cfg['influxdb']['token'],
                                           org=self.cfg['influxdb']['org'])
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

        if attr in ('get', 'put', 'post', 'delete', 'options'):
            if self.AuthError:
                self.update_headers()
            elif not self.valid_request():
                raise SessionException('method {} error'.format(attr))

        return method

    def load_items(self, filename, items_dict=False):
        self.Items = []
        self.ItemsDict = {}
        with open(os.path.expanduser(filename)) as f:
            self.Items = yaml.safe_load(f)

        # Define Default Arrays
        for item in self.Items:
            # any excludePositions
            item['excludePositions'] = item['excludePositions'] if 'excludePositions' in item \
                else []
            item['rating'] = item[
                'rating'] if 'rating' in item else 0  # any rating
            # any resourceId
            item['resourceId'] = item[
                'resourceId'] if 'resourceId' in item else 0

            if items_dict:
                try:
                    # or item['price'] < 0:
                    if item['definitionId'] in self.ItemsDict:
                        self.log({'wrong item': item})
                        return False
                except (KeyError, ValueError, TypeError):
                    self.log({'wrong item': item})
                    return False

                self.ItemsDict[item['definitionId']] = item

        return True

    def log_request(self, r, level='debug'):
        if level not in ['info', 'debug']:
            return

        self.SaveToInflux('request',
                          fields={
                              'size': len(r.content),
                              'request_time': r.elapsed.total_seconds(),
                          },
                          tags={
                              'status_code': r.status_code,
                              'url': r.request.url,
                              'uri': r.request.url.split('?')[0],
                              'action': r.request.method,
                          })

        logdata = {
            'action': r.request.method,
            'url': r.request.url,
            'uri': r.request.url.split('?')[0],
            'args': dict(parse.parse_qsl(parse.urlsplit(r.request.url).query)),
            'headers': dict(r.headers),
            'body': jsonize(r.request.body),
            'response': {
                'status': r.status_code,
                'headers': dict(r.headers),
                'data': jsonize(r.text),
            }
        }

        self.log(logdata, level=level)

    def get_headers_from_app(self):
        print(self.requests.headers)
        for h in self.requests.headers:
            self.log(self.app['headers'], level='debug')
            if h in self.app['headers']:
                self.requests.headers[h] = self.app['headers'][h]

    def update_headers(self):
        self.log({
            'headers': '{}'.format(self.app['headers']),
        })
        while not self.app or \
                self.SID_NAME not in self.app['headers'] or \
                self.app['headers'][self.SID_NAME] == self.invalid_sid:

            try:
                self.log({
                    'text': 'wait new headers from plugin',
                    'old_sid': self.requests.headers[self.SID_NAME],
                    'new_sid': self.app['headers'][self.SID_NAME],
                })
            except (KeyError, AttributeError, TypeError):
                self.log({
                    'text': 'wait first headers from plugin',
                })
            sleep(1)

            self.get_headers_from_app()

        self.log({
            'text': 'wait first headers from plugin',
        })

    def valid_request(self):
        logdata = {
            'headers': dict(self.requests.headers),
            'now': time(),
            'FailRequestInterval': self.FailRequestInterval,
            'SID_NAME': self.SID_NAME,
        }

        try:
            self.requests.headers[self.SID_NAME]
            self.log(logdata, level='debug')
        except (KeyError, AttributeError):
            self.log(logdata)
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
        # 471 - can't buy pack, cose you have unassigned items
        # 475 - trade state closed
        # 478 - NO_TRADE_EXISTS
        # 482 - invalide cookie
        # 494 - new account transfer market locked
        # 495 - try to quicksell item wich have already sold
        if r.status_code in [401, 403, 458, 426, 459]:
            self.log_request(r, level='info')
            self.AuthError = True
            # let's save invalid SID
            self.invalid_sid = self.requests.headers[self.SID_NAME]
        elif r.status_code in [
                409,
        ]:
            self.log_request(r, level='info')
        elif r.status_code in [
                471,
                495,
        ]:
            self.log_request(r, level='info')
        elif r.status_code in [
                200,
                461,
                478,
        ]:
            self.AuthError = False
            self.log_request(r)
        else:
            self.log_request(r, level='info')
            raise SessionException('UT API Error')

    def get(self, url, params={}):
        r = self.requests.get(url, params=params)
        self.response_handler(r)
        return r

    def delete(self, url):
        r = self.requests.delete(url)
        self.response_handler(r)
        return r

    def options(self, url):
        r = self.requests.options(url)
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

        items = auction_info_items(r)
        if len(items) == 0 and \
                'start' in params and \
                params['start'] == 0:
            self.empty_searches += 1

        return items

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
            if 'maxb' in params:
                params['maxb'] = blur_price(params['maxb'], -0.97)
                if 'minb' in params:
                    params['minb'] = blur_price(params['maxb'], 0.4)
            params['start'] = self.cfg['market_page_size'] * page
            return self.search(params)
        except (IndexError, KeyError):
            return {}

    def club(self, params={
        'start': 0,
    }):
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
            p = self.club(params={'start': page * self.cfg['club_page_size']})
            players += p
            page += 1

            if len(p) < self.cfg['club_page_size']:
                return players

        return players

    def ItemSuited(self, index, item):
        if item['itemData']['itemType'] == 'player':
            if item['itemData']['rating'] < self.Items[index]['rating']:
                return False

            if item['itemData']['preferredPosition'] in self.Items[index][
                    'excludePositions']:
                return False

            if 'profit' in self.Items[index]:
                profit = self.GetExternalPrice(item['itemData']['resourceId']
                                               ) * 0.95 - item['buyNowPrice']
                self.log({'potential_profit': profit, 'credits': self.credits})
                if profit >= self.Items[index]['profit']:
                    return True
                else:
                    return False
            elif item['buyNowPrice'] <= self.Items[index]['params']['maxb']:
                return True

        if item['itemData']['itemType'] == 'training' and \
                item['itemData']['cardsubtypeid'] in [220, 107, 108, 268, 266, 262]:
            return True

        return False

    def set_credits(self, credits):
        """ dumb function for budget calculatein in future """
        self.credits = int(credits)
        self.SaveToInflux('credits', fields={'total': self.credits}, tags={})

    def Bid(self, tradeId, bid):
        r = self.put(
            self.cfg['urls']['bid'].format(tradeId),
            json={'bid': bid},
        )

        try:
            self.set_credits(r.json()['credits'])
        except (KeyError, json.decoder.JSONDecodeError,
                simplejson.errors.JSONDecodeError):
            pass

        if r.status_code != 200:
            return False

        if self.bid_limit:
            self.bid_limit -= 1

        self.log({'bid_limit': self.bid_limit, 'credits': self.credits})
        return True

    def SaveItem(self, item):
        if self.influx_write_client:
            self.influx_write_client.write(
                self.cfg['influxdb']['bucket'], self.cfg['influxdb']['org'], {
                    "measurement": "items",
                    "tags": itemdata2tags(item['itemData']),
                    "fields": {
                        "buynow": item['buyNowPrice']
                    },
                    "time": time_ns(),
                })

    def SaveToInflux(self, measurement, fields, tags={}):
        if self.influx_write_client:
            self.influx_write_client.write(
                self.cfg['influxdb']['bucket'], self.cfg['influxdb']['org'], {
                    'measurement': measurement,
                    "tags": tags,
                    'fields': fields,
                    'time': time_ns(),
                })

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

            if len(items
                   ) <= self.cfg['market_page_size']:  # last not empty page
                break

            if page == self.cfg[
                    'market_page_limit'] - 1:  # all 3 pages were full
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
                    random_sleep(0.5, 1)
                if self.bid_limit <= 0:
                    return

            if len(items) < self.cfg['market_page_size']:  # last page
                break

        random_sleep(1, 4)

    def UpdateCredits(self):
        try:
            self.set_credits(
                self.get(self.cfg['urls']['credits']).json()['credits'])
        except (KeyError, ValueError):
            self.log({'message': "Can't get credits"})

    def BuyPack(self, packId=100):
        self.log({
            'packId': packId,
        })

        r = self.post(
            self.cfg['urls']['purchased_items'],
            json={
                'packId': packId,
                'currency': 'COINS',
            },
        )

        if r.status_code != 200:
            self.buy_pack_fails += 1
            if self.buy_pack_fails > 1:
                self.log_request(r)
                raise SessionException('BuyPack API Error')
            return []

        self.buy_pack_fails = 0
        self.SaveToInflux('pack', fields={'buyed': 1}, tags={'packId': packId})

        try:
            return r.json()['itemData']
        except KeyError:
            return []

    def BuyRandomItem(self):
        self.BuyItemByIndex(randint(0, len(self.Items) - 1))

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
                'https://www.futbin.com/20/playerPrices?player={}'.format(
                    resourceId))
            price = int(r.json()[str(resourceId)]['prices'][platform]
                        [price_type].replace(',', ''))
            self.prices_cache[resourceId] = price
        except (KeyError, TypeError, json.decoder.JSONDecodeError):
            return 0

        return price

    def GetFutcardsPrice(self, resourceId, platform='ps'):
        if resourceId in self.prices_cache:
            return self.prices_cache[resourceId]['price']

        try:
            r = requests.get(
                'https://futcards.info/api/cards/price/free/{}'.format(
                    resourceId))
            player_info = r.json()[str(resourceId)]['prices'][platform]
            price = int(player_info['price'])

            # QuckSell item if price isn't fresh enought
            if (int(player_info['actual']) >= self.actual_price_time
                    and price <= self.quick_sell_price):
                return 0

            self.prices_cache[resourceId] = player_info
        except (KeyError, TypeError, json.decoder.JSONDecodeError):
            return 0

        return price

    def GetExternalPrice(self, resourceId):
        if self.futbin:
            return self.GetFutbinPrice(resourceId)

        # self.futcards and not setted
        return self.GetFutcardsPrice(resourceId)

    def GetPlayerPrice(self, item_data):
        if self.futbin or self.futcards:
            return self.GetExternalPrice(item_data['resourceId'])
        else:
            return self.GetItemByResourseId(item_data['resourceId'])['price']

    def GetPrice(self, item_data):
        if item_data['itemType'] == 'player':
            return self.GetPlayerPrice(item_data)
        # clubInfo - price 0
        # stuff - price 0
        # 5002004 - bronze squad fitness
        # 5002005 - silver squad fitness
        # if item_data['itemType'] == 'development' and \
        #        item_data['definitionId'] in [5002004, 5002005, 5002006]:
        #    return 1300

        if 'definitionId' in item_data:
            itemId = item_data['definitionId']
            self.log({'item_data_with_definitionId': item_data})
        elif 'resourceId' in item_data:
            itemId = item_data['resourceId']

        try:
            return self.ItemsDict[itemId]['price']
        except (KeyError, AttributeError):
            self.log({
                'price_not_found': {
                    'itemId': itemId,
                    'item_data': item_data,
                }
            })

        return 0

    def MoveToPile(self, item_data, pile='trade'):
        # default pile is trade
        if pile not in ('trade', 'club'):
            return False

        try:
            r = self.put(
                self.cfg['urls']['item'],
                json={
                    'itemData': [
                        {
                            'id': item_data['id'],
                            'pile': pile,
                        },
                    ],
                },
            )
        except TypeError:
            print('--- Item ---')
            print(item_data)
            print('------------')
            print(pile)
            sys.exit(1)

        if r.status_code != 200:
            return False
        return True

    def RedeamReward(self, item_data):
        r = self.post('{}/{}'.format(self.cfg['urls']['item'],
                                     item_data['id']),
                      json={'apply': []})
        if r.status_code != 200:
            return False
        return True

    def ProcessPurchasedItem(self, item_data):
        price = self.GetPrice(item_data)
        if item_data['itemType'] == 'misc':
            # Redeam reward if its a misc like Gold or Draft ...
            self.RedeamReward(item_data)
        elif price == 0:
            self.PutToQuickSell(item_data)
            return False
        elif price < 0:
            return self.MoveToPile(item_data, 'club')
        else:
            return self.MoveToPile(item_data, 'trade')
        return True

    def ClearSold(self):
        r = self.delete(self.cfg['urls']['sold'])
        self.transfer_closed = False
        if r.status_code != 200:
            return False
        return True

    def QuickSellItem(self, item):
        item_data = pure_item(item)
        r = self.delete('{}/{}'.format(self.cfg['urls']['item'],
                                       item_data['id']))
        if r.status_code != 200:
            return False

        return True

    def PutToQuickSell(self, item_data):
        self.quick_sell_ids.append(item_data['id'])
        return False

    def QuickSellItems(self):
        if not self.quick_sell_ids:
            return True

        r = self.post(
            self.cfg['urls']['delete'],
            json={
                'itemId': self.quick_sell_ids,
            },
        )

        if r.status_code != 200:
            return False

        self.quick_sell_ids = []
        return True

    def Auction(self, item):
        item_data = pure_item(item)
        if item['tradeState'] in [
                'active',
        ]:
            return False
        if item['tradeState'] == 'closed':
            self.transfer_closed = True
            return False
        # self.log({'debug': item_data})

        min_price = item_data['marketDataMinPrice']
        if item['tradeState'] == 'expired' and\
           min_price + delta_by_price(min_price) >= item['buyNowPrice']:
            return self.QuickSellItem(item)

        price = self.GetPrice(item_data)
        if price == 0:
            return self.QuickSellItem(item)

        start = blur_price(price, 0.7, min_price=min_price)
        buynow = price

        if buynow > item_data['marketDataMaxPrice']:
            buynow = item_data['marketDataMaxPrice']

        self.log({
            'item': item_data,
            'trytosell': buynow,
        })
        r = self.post(
            self.cfg['urls']['auction'],
            json={
                'itemData': {
                    'id': item_data['id'],
                },
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

    def GetPurchasedItems(self):
        try:
            r = self.get(
                self.cfg['urls']['purchased_items']).json()['itemData']
            return r
        except KeyError:
            return []
        except json.decoder.JSONDecodeError:
            self.log_request(r)
            raise SessionException('get purchased_items error')

    def MovePurchasedItems(self):
        for item_data in self.GetPurchasedItems():
            random_sleep(1, 2)
            self.log({'purchased_item': item_data})
            self.ProcessPurchasedItem(item_data)

        self.QuickSellItems()
        self.purchased_count = 0

    def SellFromTradePile(self):
        for item in self.tradepile():
            if self.Auction(item):
                random_sleep(2, 4)

    def DecodeSearchUrl(self, url):
        r = self.get(url)
        try:
            first = r.json()['auctionInfo'][0]
        except (
                KeyError,
                IndexError,
        ):
            self.log('Try one more time, or increase maxb')
            self.exit()

        # print(json.dumps(first))
        item_tmpl = [{
            'name':
            'OptionalValue',
            'resourceId':
            first['itemData']['resourceId'],
            'params':
            dict(parse.parse_qsl(parse.urlsplit(r.request.url).query)),
            'price':
            first['buyNowPrice'],
        }]

        if first['itemData']['itemType'] == 'player':
            item_tmpl[0]['rating'] = 'OptionalValue'

        print(yaml.dump(item_tmpl))

    def aiohttp_server(self):

        def http_get(request):
            self.app['headers'].update(dict(request.headers))
            headers = {'Access-Control-Allow-Origin': '*'}
            return web.Response(text='OK', headers=headers)

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
        # original_loop = asyncio.get_event_loop()
        # original_loop.run_until_complete(site.start())
        self.loop.run_until_complete(site.start())
        self.loop.run_forever()

    def stop(self):
        self.log('STOP')
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


def main():
    parser = argparse.ArgumentParser(description='Fifa Config Parser')
    parser.add_argument('-c',
                        '--config',
                        type=str,
                        default='fifa23.yaml',
                        help='config yaml file')
    parser.add_argument('-i', '--items', type=str, help='items yaml file')
    parser.add_argument(
        '--decode-url',
        type=str,
        default='',
        help='decode search url copied from browser debug console')
    parser.add_argument('--tries',
                        type=int,
                        default=sys.maxsize,
                        help='how many times we will try to buy an item')
    parser.add_argument('--quick-sell-price',
                        type=int,
                        default=100,
                        help='Maximun QuckSell item price')
    parser.add_argument('--bid-limit',
                        type=int,
                        default=1,
                        help='how many items we will buy')
    parser.add_argument('--pack',
                        type=int,
                        default=0,
                        choices=[100, 101, 200, 201, 300, 301],
                        help='Pack type for buying')
    # 100 - bronze 400
    # 301 - gold 7500
    parser.add_argument('--dump', dest='dump', action='store_true')
    parser.add_argument('--web', dest='web', action='store_true')
    parser.add_argument('--futbin', dest='futbin', action='store_true')
    parser.add_argument('--futcards', dest='futcards', action='store_true')
    parser.add_argument('--no-futcards', dest='futcards', action='store_false')
    parser.add_argument('--buy', dest='buy', action='store_true')
    parser.add_argument('--sell', dest='sell', action='store_true')
    parser.add_argument('-v', '--verbose', dest='debug', action='store_true')
    parser.set_defaults(buy=False)
    parser.set_defaults(sell=False)
    parser.set_defaults(decode_url=False)
    parser.set_defaults(debug=False)
    args = parser.parse_args()

    # Create instance and fillup the pararms
    fifa = FifaWeb(args.config)
    if args.items and not fifa.load_items(
            args.items, items_dict=True if args.pack else False):
        fifa.log('Can\'t parse item yaml')
        sys.exit(1)
    fifa.bid_limit = args.bid_limit
    fifa.quick_sell_price = args.quick_sell_price
    if args.debug:
        fifa.logger.setLevel(logging.DEBUG)

    if args.futbin:
        fifa.futbin = args.futbin
        fifa.futcards = args.futcards

    if args.web:
        t = threading.Thread(target=fifa.run_server,
                             args=(fifa.aiohttp_server(), ))
        t.start()

        fifa.update_headers()

    # Choose the active action
    if args.pack:
        for i in range(args.tries):
            if args.buy:
                fifa.BuyPack(args.pack)
            random_sleep(1, 2)
            if args.sell:
                fifa.UpdateCredits()
                fifa.MovePurchasedItems()
                fifa.SellFromTradePile()
                if fifa.transfer_closed:
                    fifa.ClearSold()

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
            random_sleep(1, 2)
            # try to sell all items once per 5 purchase
            if args.sell and fifa.purchased_count and not fifa.purchased_count % 5:
                fifa.MovePurchasedItems()
                fifa.SellFromTradePile()

            # Long sleep after to many empty searches
            if not fifa.empty_searches % 10:
                random_sleep(30, 90)
            if fifa.bid_limit <= 0:
                break

    if args.sell:
        fifa.MovePurchasedItems()
        fifa.SellFromTradePile()

    if args.decode_url:
        fifa.DecodeSearchUrl(args.decode_url)

    fifa.stop()


if __name__ == '__main__':
    try:
        main()
    except SessionException:
        os.system('kill %d' % os.getpid())
