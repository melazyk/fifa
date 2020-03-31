#!/usr/bin/env python3
import sys
import os
import requests
import yaml
import json
from random import randint, uniform
from time import sleep, time_ns
import argparse
import logging
from urllib import parse
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import ASYNCHRONOUS


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


class FifaWeb(object):
    def __init__(self, config_file):
        with open(os.path.expanduser(config_file)) as f:
            self.cfg = yaml.safe_load(f)

        self.Log = logging.getLogger("fifa_log")
        self.Log.setLevel(logging.INFO)
        if 'logfile' in self.cfg:
            fh = logging.FileHandler(self.cfg['logfile'])
        else:
            fh = logging.StreamHandler()

        fh.setFormatter(logging.Formatter(
            '{"time": "%(asctime)s", "name": "%(name)s", \
                    "level": "%(levelname)s", "message": %(message)s }'
        ))
        self.Log.addHandler(fh)

        if 'influxdb' in self.cfg:
            self.influxdb = InfluxDBClient(
                url=self.cfg['influxdb']['url'],
                token=self.cfg['influxdb']['token'],
                org=self.cfg['influxdb']['org']
            )
            self.influx_write_client = self.influxdb.write_api(
                write_options=ASYNCHRONOUS)

        # save urls for bot
        self.cfg['urls'] = {
            'market':             self.cfg['base_url'] + 'transfermarket',
            'bid':                self.cfg['base_url'] + 'trade/{}/bid',
            'purchased_items':    self.cfg['base_url'] + 'purchased/items',
            'item':               self.cfg['base_url'] + 'item',
            'auction':            self.cfg['base_url'] + 'auctionhouse',
        }

        self.requests = requests.Session()
        self.requests.headers.update(self.cfg['headers'])

    def LoadItems(self, filename):
        self.Items = []
        with open(os.path.expanduser(filename)) as f:
            self.Items = yaml.safe_load(f)

    def LogRequest(self, r, level='debug'):
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

        # call self.{{level}} function
        getattr(self, level)(logdata)

    def get(self, url, params={}):
        r = self.requests.get(url, params=params)
        self.LogRequest(r)
        if r.status_code != 200:
            self.LogRequest(r, level='info')
            sys.exit(1)
        return r

    def put(self, url, json):
        r = self.requests.put(url, json=json)
        self.LogRequest(r)
        return r

    def post(self, url, json):
        r = self.requests.post(url, json=json)
        self.LogRequest(r)
        return r

    def info(self, message):
        self.Log.info(json.dumps(message))

    def debug(self, message):
        self.Log.debug(json.dumps(message))

    def search(self, params):
        payload = self.cfg['params'].copy()
        payload.update(params)
        self.info(payload)
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
        if item['itemData']['itemType'] == 'player' and \
                item['itemData']['rating'] >= self.Items[index]['rating'] and \
                item['buyNowPrice'] <= self.Items[index]['prices']['buy_limit']:
            return True
        elif item['itemData']['itemType'] == 'training' and \
                item['itemData']['cardsubtypeid'] in [220, 107, 108, 268, 266, 262]:
            return True

        return False

    def BidItem(self, tradeId, bid):
        print("tradeId: {}, bid: {}".format(tradeId, bid))
        self.info({
            'tradeId': tradeId,
            'bid': bid,
        })

        r = self.put(
            self.cfg['urls']['bid'].format(tradeId),
            json={'bid': bid},
        )

        if r.status_code != 200:
            return False

        if self.BuyCounter:
            self.BuyCounter -= 1
        if not self.BuyCounter:
            self.info("BuyCounter is 0. Exit.")
            sys.exit(1)

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
                maxb = move_maxb(maxb, 1.05, delta=-100)
                break

            for item in items:
                self.SaveItem(item)
                # self.info(item)

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
                    self.BidItem(item['tradeId'], item['buyNowPrice'])

            if len(items) < self.cfg['market_page_size']:  # last page
                break

            random_sleep(0.5, 1.5)

    def BuyRandomItem(self):
        self.BuyItemByIndex(randint(0, len(self.Items)-1))

    def GetPurchasedItems(self):
        try:
            return self.get(self.cfg['urls']['purchased_items']).json()['itemData']
        except KeyError:
            return []

    def GetItemByResourseId(self, resourceId):
        for item in self.Items:
            if item['resourceId'] == resourceId:
                return item
        return {}

    def MoveToTradePill(self, item):
        r = self.put(self.cfg['urls']['item'],
                     json={'itemData': [{'id': item['id'],
                                         'pile': 'trade',
                                         }, ],
                           },
                     )

        print(r.status_code, r.text)
        if r.status_code != 200:
            return False

        return True

    def Auction(self, item):
        prices = self.GetItemByResourseId(item['resourceId'])['prices']
        print(self.cfg['urls']['auction'])
        r = self.post(
            self.cfg['urls']['auction'],
            json={'itemData':
                  {'id': item['id'], },
                  'startingBid': prices['start'],
                  'duration': 3600,
                  'buyNowPrice': prices['buynow'],
                  },
        )

        print(r.status_code, r.text)
        if r.status_code != 200:
            return False

        return True

    def ItemCanBeSold(self, item):
        s_item = self.GetItemByResourseId(item['resourceId'])
        print(s_item)
        try:
            prices = self.GetItemByResourseId(item['resourceId'])['prices']
        except KeyError:
            pass

        print(prices)
        # if prices exist and greather than 0
        if ('start' in prices and 'buynow' in prices
                and int(prices['start']) > 0 and int(prices['buynow']) > 0):
            return True

        return False

    def SellPurchasedItems(self):
        for purchased_item in self.GetPurchasedItems():
            print(purchased_item)
            random_sleep(5, 15)
            if self.ItemCanBeSold(purchased_item):
                print("CanBeSold")
                if self.MoveToTradePill(purchased_item):
                    random_sleep(0.4, 1)
                    self.Auction(purchased_item)
                    random_sleep(5, 10)

    def DecodeSearchUrl(self, url):
        r = self.get(url)
        try:
            first = r.json()['auctionInfo'][0]
        except (KeyError, IndexError, ):
            self.info('Try one more time, or increase maxb')
            sys.exit(1)

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
            item_tmpl[0]['prices']['buy_limit'] = first['startingBid']

        print(yaml.dump(item_tmpl))


def main():
    parser = argparse.ArgumentParser(description='Fifa Config Parser')
    parser.add_argument('-c', '--config', type=str,
                        help='config yaml file', required=True)
    parser.add_argument('-i', '--items', type=str, help='items yaml file')
    parser.add_argument('--decode-url', type=str, default='',
                        help='decode search url copied from browser debug console')
    parser.add_argument('--tries', type=int, default=900,
                        help='how many times we will try to buy an item')
    parser.add_argument('--buy-count', type=int, default=5,
                        help='how many items we will buy')
    parser.add_argument('--dump', dest='dump', action='store_true')
    parser.add_argument('--buy', dest='buy', action='store_true')
    parser.add_argument('--sell', dest='sell', action='store_true')
    parser.add_argument('-v', '--verbose', dest='debug', action='store_true')
    parser.set_defaults(buy=False)
    parser.set_defaults(sell=False)
    parser.set_defaults(decode_url=False)
    parser.set_defaults(debug=False)
    args = parser.parse_args()

    fifa = FifaWeb(args.config)
    fifa.LoadItems(args.items)
    fifa.BuyCounter = args.buy_count

    if args.debug:
        fifa.Log.setLevel(logging.DEBUG)

    if args.dump:
        maxb = 0  # set default value from item yaml
        for i in range(args.tries):
            fifa.info({"attempt": i})
            # save maxb from preview search
            maxb = fifa.DumpItemByIndex(0, maxb)
            fifa.info('next price {}'.format(maxb))
            random_sleep(5, 15)

    if args.buy:
        for i in range(args.tries):
            fifa.info({"attempts": i})
            fifa.BuyRandomItem()
            random_sleep(5, 15)

    if args.sell:
        fifa.SellPurchasedItems()

    if args.decode_url:
        fifa.DecodeSearchUrl(args.decode_url)


if __name__ == '__main__':
    main()
