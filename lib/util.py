import time
import decimal
import sys
import json
import logging
import apsw
import collections
import inspect
import requests
from datetime import datetime
from dateutil.tz import tzlocal
from operator import itemgetter
import fractions
import warnings
import binascii
import hashlib
import sha3
from functools import lru_cache
import getpass
import bitcoin as bitcoinlib
import os

from . import (config, exceptions, backend)
from .exceptions import DecodeError

D = decimal.Decimal
b26_digits = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
b58_digits = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

dhash = lambda x: hashlib.sha256(hashlib.sha256(x).digest()).digest()

json_print = lambda x: print(json.dumps(x, sort_keys=True, indent=4))

# Obsolete in Python 3.4, with enum module.
BET_TYPE_NAME = {0: 'BullCFD', 1: 'BearCFD', 2: 'Equal', 3: 'NotEqual'}
BET_TYPE_ID = {'BullCFD': 0, 'BearCFD': 1, 'Equal': 2, 'NotEqual': 3}

BLOCK_LEDGER = []

CURR_DIR = os.path.dirname(os.path.realpath(__file__))
with open(CURR_DIR + '/../protocol_changes.json') as f:
    PROTOCOL_CHANGES = json.load(f)

class RPCError (Exception): pass

# TODO: Move to `counterparty-cli.py`.
# TODO: This doesn’t timeout properly. (If server hangs, then unhangs, no result.)
def api (method, params):
    headers = {'content-type': 'application/json'}
    payload = {
        "method": method,
        "params": params,
        "jsonrpc": "2.0",
        "id": 0,
    }
    response = requests.post(config.RPC, data=json.dumps(payload), headers=headers)
    if response == None:
        raise RPCError('Cannot communicate with {} server.'.format(config.XCP_CLIENT))
    elif response.status_code != 200:
        if response.status_code == 500:
            raise RPCError('Malformed API call.')
        else:
            raise RPCError(str(response.status_code) + ' ' + response.reason)

    response_json = response.json()
    if 'error' not in response_json.keys() or response_json['error'] == None:
        try:
            return response_json['result']
        except KeyError:
            raise RPCError(response_json)
    else:
        raise RPCError('{}'.format(response_json['error']))


def price (numerator, denominator, block_index):
    if block_index >= 294500 or config.TESTNET: # Protocol change.
        return fractions.Fraction(numerator, denominator)
    else:
        numerator = D(numerator)
        denominator = D(denominator)
        return D(numerator / denominator)

def log (db, command, category, bindings):
    cursor = db.cursor()

    for element in bindings.keys():
        try:
            str(bindings[element])
        except Exception:
            bindings[element] = '<Error>'

    # Slow?!
    def output (quantity, asset):
        try:
            if asset not in ('fraction', 'leverage'):
                return str(value_out(db, quantity, asset)) + ' ' + asset
            else:
                return str(value_out(db, quantity, asset))
        except exceptions.AssetError:
            return '<AssetError>'
        except decimal.DivisionByZero:
            return '<DivisionByZero>'
        except TypeError:
            return '<None>'

    if command == 'update':
        if category == 'order':
            logging.debug('Database: set status of order {} to {}.'.format(bindings['tx_hash'], bindings['status']))
        elif category == 'bet':
            logging.debug('Database: set status of bet {} to {}.'.format(bindings['tx_hash'], bindings['status']))
        elif category == 'order_matches':
            logging.debug('Database: set status of order_match {} to {}.'.format(bindings['order_match_id'], bindings['status']))
        elif category == 'bet_matches':
            logging.debug('Database: set status of bet_match {} to {}.'.format(bindings['bet_match_id'], bindings['status']))
        # TODO: elif category == 'balances':
            # logging.debug('Database: set balance of {} in {} to {}.'.format(bindings['address'], bindings['asset'], output(bindings['quantity'], bindings['asset']).split(' ')[0]))

    elif command == 'insert':

        if category == 'credits':
            logging.debug('Credit: {} to {} #{}# <{}>'.format(output(bindings['quantity'], bindings['asset']), bindings['address'], bindings['action'], bindings['event']))

        elif category == 'debits':
            logging.debug('Debit: {} from {} #{}# <{}>'.format(output(bindings['quantity'], bindings['asset']), bindings['address'], bindings['action'], bindings['event']))

        elif category == 'sends':
            logging.info('Send: {} from {} to {} ({}) [{}]'.format(output(bindings['quantity'], bindings['asset']), bindings['source'], bindings['destination'], bindings['tx_hash'], bindings['status']))

        elif category == 'orders':
            logging.info('Order: {} ordered {} for {} in {} blocks, with a provided fee of {} {} and a required fee of {} {} ({}) [{}]'.format(bindings['source'], output(bindings['give_quantity'], bindings['give_asset']), output(bindings['get_quantity'], bindings['get_asset']), bindings['expiration'], bindings['fee_provided'] / config.UNIT, config.BTC, bindings['fee_required'] / config.UNIT, config.BTC, bindings['tx_hash'], bindings['status']))

        elif category == 'order_matches':
            logging.info('Order Match: {} for {} ({}) [{}]'.format(output(bindings['forward_quantity'], bindings['forward_asset']), output(bindings['backward_quantity'], bindings['backward_asset']), bindings['id'], bindings['status']))

        elif category == 'btcpays':
            logging.info('{} Payment: {} paid {} to {} for order match {} ({}) [{}]'.format(config.BTC, bindings['source'], output(bindings['btc_amount'], config.BTC), bindings['destination'], bindings['order_match_id'], bindings['tx_hash'], bindings['status']))

        elif category == 'issuances':
            if bindings['transfer']:
                logging.info('Issuance: {} transfered asset {} to {} ({}) [{}]'.format(bindings['source'], bindings['asset'], bindings['issuer'], bindings['tx_hash'], bindings['status']))
            elif bindings['locked']:
                logging.info('Issuance: {} locked asset {} ({}) [{}]'.format(bindings['issuer'], bindings['asset'], bindings['tx_hash'], bindings['status']))
            else:
                if bindings['divisible']:
                    divisibility = 'divisible'
                    unit = config.UNIT
                else:
                    divisibility = 'indivisible'
                    unit = 1
                if bindings['callable'] and (bindings['block_index'] > 283271 or config.TESTNET):   # Protocol change.
                    callability = 'callable from {} for {} XCP/{}'.format(isodt(bindings['call_date']), bindings['call_price'], bindings['asset'])
                else:
                    callability = 'uncallable'
                try:
                    quantity = value_out(db, bindings['quantity'], None, divisible=bindings['divisible'])
                except Exception as e:
                    quantity = '?'
                logging.info('Issuance: {} created {} of asset {}, which is {} and {} ({}) [{}]'.format(bindings['issuer'], quantity, bindings['asset'], divisibility, callability, bindings['tx_hash'], bindings['status']))

        elif category == 'broadcasts':
            if bindings['locked']:
                logging.info('Broadcast: {} locked his feed ({}) [{}]'.format(bindings['source'], bindings['tx_hash'], bindings['status']))
            else:
                logging.info('Broadcast: ' + bindings['source'] + ' at ' + isodt(bindings['timestamp']) + ' with a fee of {}%'.format(output(D(bindings['fee_fraction_int'] / 1e8) * D(100), 'fraction')) + ' (' + bindings['tx_hash'] + ')' + ' [{}]'.format(bindings['status']))

        elif category == 'bets':
            logging.info('Bet: {} against {}, by {}, on {}'.format(output(bindings['wager_quantity'], config.XCP), output(bindings['counterwager_quantity'], config.XCP), bindings['source'], bindings['feed_address']))

        elif category == 'bet_matches':
            placeholder = ''
            if bindings['target_value'] >= 0:    # Only non‐negative values are valid.
                placeholder = ' that ' + str(output(bindings['target_value'], 'value'))
            if bindings['leverage']:
                placeholder += ', leveraged {}x'.format(output(bindings['leverage'] / 5040, 'leverage'))
            logging.info('Bet Match: {} for {} against {} for {} on {} at {}{} ({}) [{}]'.format(BET_TYPE_NAME[bindings['tx0_bet_type']], output(bindings['forward_quantity'], config.XCP), BET_TYPE_NAME[bindings['tx1_bet_type']], output(bindings['backward_quantity'], config.XCP), bindings['feed_address'], isodt(bindings['deadline']), placeholder, bindings['id'], bindings['status']))

        elif category == 'dividends':
            logging.info('Dividend: {} paid {} per unit of {} ({}) [{}]'.format(bindings['source'], output(bindings['quantity_per_unit'], bindings['dividend_asset']), bindings['asset'], bindings['tx_hash'], bindings['status']))

        elif category == 'burns':
            logging.info('Burn: {} burned {} for {} ({}) [{}]'.format(bindings['source'], output(bindings['burned'], config.BTC), output(bindings['earned'], config.XCP), bindings['tx_hash'], bindings['status']))

        elif category == 'cancels':
            logging.info('Cancel: {} ({}) [{}]'.format(bindings['offer_hash'], bindings['tx_hash'], bindings['status']))

        elif category == 'callbacks':
            logging.info('Callback: {} called back {} of {} ({}) [{}]'.format(bindings['source'], value_out(db, bindings['fraction'], 'fraction'), bindings['asset'], bindings['tx_hash'], bindings['status']))

        elif category == 'rps':
            log_message = 'RPS: {} opens game with {} possible moves and a wager of {}'.format(bindings['source'], bindings['possible_moves'], output(bindings['wager'], 'XCP'))
            logging.info(log_message)

        elif category == 'rps_matches':
            log_message = 'RPS Match: {} is playing a {}-moves game with {} with a wager of {} ({}) [{}]'.format(bindings['tx0_address'], bindings['possible_moves'], bindings['tx1_address'], output(bindings['wager'], 'XCP'), bindings['id'], bindings['status'])
            logging.info(log_message)

        elif category == 'rpsresolves':

            if bindings['status'] == 'valid':
                rps_matches = list(cursor.execute('''SELECT * FROM rps_matches WHERE id = ?''', (bindings['rps_match_id'],)))
                assert len(rps_matches) == 1
                rps_match = rps_matches[0]
                log_message = 'RPS Resolved: {} is playing {} on a {}-moves game with {} with a wager of {} ({}) [{}]'.format(rps_match['tx0_address'], bindings['move'], rps_match['possible_moves'], rps_match['tx1_address'], output(rps_match['wager'], 'XCP'), rps_match['id'], rps_match['status'])
            else:
                log_message = 'RPS Resolved: {} [{}]'.format(bindings['tx_hash'], bindings['status'])
            logging.info(log_message)

        elif category == 'order_expirations':
            logging.info('Expired order: {}'.format(bindings['order_hash']))

        elif category == 'order_match_expirations':
            logging.info('Expired Order Match awaiting payment: {}'.format(bindings['order_match_id']))

        elif category == 'bet_expirations':
            logging.info('Expired bet: {}'.format(bindings['bet_hash']))

        elif category == 'bet_match_expirations':
            logging.info('Expired Bet Match: {}'.format(bindings['bet_match_id']))

        elif category == 'bet_match_resolutions':
            # DUPE
            cfd_type_id = BET_TYPE_ID['BullCFD'] + BET_TYPE_ID['BearCFD']
            equal_type_id = BET_TYPE_ID['Equal'] + BET_TYPE_ID['NotEqual']

            if bindings['bet_match_type_id'] == cfd_type_id:
                if bindings['settled']:
                    logging.info('Bet Match Settled: {} credited to the bull, {} credited to the bear, and {} credited to the feed address ({})'.format(output(bindings['bull_credit'], config.XCP), output(bindings['bear_credit'], config.XCP), output(bindings['fee'], config.XCP), bindings['bet_match_id']))
                else:
                    logging.info('Bet Match Force‐Liquidated: {} credited to the bull, {} credited to the bear, and {} credited to the feed address ({})'.format(output(bindings['bull_credit'], config.XCP), output(bindings['bear_credit'], config.XCP), output(bindings['fee'], config.XCP), bindings['bet_match_id']))

            elif bindings['bet_match_type_id'] == equal_type_id:
                logging.info('Bet Match Settled: {} won the pot of {}; {} credited to the feed address ({})'.format(bindings['winner'], output(bindings['escrow_less_fee'], config.XCP), output(bindings['fee'], config.XCP), bindings['bet_match_id']))

        elif category == 'rps_expirations':
            logging.info('Expired RPS: {}'.format(bindings['rps_hash']))

        elif category == 'rps_match_expirations':
            logging.info('Expired RPS Match: {}'.format(bindings['rps_match_id']))

        elif category == 'contracts':
            logging.info('New Contract: {}'.format(bindings['contract_id']))

        elif category == 'executions':
            """
            try:
                payload_hex = binascii.hexlify(bindings['payload']).decode('ascii')
            except TypeError:
                payload_hex = '<None>'
            try:
                output_hex = binascii.hexlify(bindings['output']).decode('ascii')
            except TypeError:
                output_hex = '<None>'
            logging.info('Execution: {} executed contract {}, funded with {}, at a price of {} (?), at a final cost of {}, reclaiming {}, and also sending {}, with a data payload of {}, yielding {} ({}) [{}]'.format(bindings['source'], bindings['contract_id'], output(bindings['gas_start'], config.XCP), bindings['gas_price'], output(bindings['gas_cost'], config.XCP), output(bindings['gas_remaining'], config.XCP), output(bindings['value'], config.XCP), payload_hex, output_hex, bindings['tx_hash'], bindings['status']))
            """
            if bindings['contract_id']:
                logging.info('Execution: {} executed contract {} ({}) [{}]'.format(bindings['source'], bindings['contract_id'], bindings['tx_hash'], bindings['status']))
            else:
                logging.info('Execution: {} created contract {} ({}) [{}]'.format(bindings['source'], bindings['output'], bindings['tx_hash'], bindings['status']))

        elif category == 'destructions':
            logging.info('Destruction: {} destroyed {} {} with tag ‘{}’({}) [{}]'.format(bindings['source'], bindings['quantity'], bindings['asset'], bindings['tag'], bindings['tx_hash'], bindings['status']))

    cursor.close()


def message (db, block_index, command, category, bindings, tx_hash=None):
    cursor = db.cursor()

    # Get last message index.
    messages = list(cursor.execute('''SELECT * FROM messages
                                      WHERE message_index = (SELECT MAX(message_index) from messages)'''))
    if messages:
        assert len(messages) == 1
        message_index = messages[0]['message_index'] + 1
    else:
        message_index = 0

    # Not to be misleading…
    if block_index == config.MEMPOOL_BLOCK_INDEX:
        try:
            del bindings['status']
            del bindings['block_index']
            del bindings['tx_index']
        except KeyError:
            pass

    # Handle binary data.
    items = []
    for item in sorted(bindings.items()):
        if type(item[1]) == bytes:
            items.append((item[0], binascii.hexlify(item[1]).decode('ascii')))
        else:
            items.append(item)

    bindings_string = json.dumps(collections.OrderedDict(items))
    cursor.execute('insert into messages values(:message_index, :block_index, :command, :category, :bindings, :timestamp)',
                   (message_index, block_index, command, category, bindings_string, curr_time()))

    # Log only real transactions.
    if block_index != config.MEMPOOL_BLOCK_INDEX:
        log(db, command, category, bindings)

    cursor.close()


def isodt (epoch_time):
    try:
        return datetime.fromtimestamp(epoch_time, tzlocal()).isoformat()
    except OSError:
        return '<datetime>'

def curr_time():
    return int(time.time())

def date_passed(date):
    return date <= time.time()

def sortkeypicker(keynames):
    """http://stackoverflow.com/a/1143719"""
    negate = set()
    for i, k in enumerate(keynames):
        if k[:1] == '-':
            keynames[i] = k[1:]
            negate.add(k[1:])
    def getit(adict):
       composite = [adict[k] for k in keynames]
       for i, (k, v) in enumerate(zip(keynames, composite)):
           if k in negate:
               composite[i] = -v
       return composite
    return getit

def last_block (db):
    cursor = db.cursor()
    blocks = list(cursor.execute('''SELECT * FROM blocks WHERE block_index = (SELECT MAX(block_index) from blocks)'''))
    if blocks:
        assert len(blocks) == 1
        last_block = blocks[0]
    else:
        raise exceptions.DatabaseError('No blocks found.')
    cursor.close()
    return last_block

def last_message (db):
    cursor = db.cursor()
    messages = list(cursor.execute('''SELECT * FROM messages WHERE message_index = (SELECT MAX(message_index) from messages)'''))
    if messages:
        assert len(messages) == 1
        last_message = messages[0]
    else:
        raise exceptions.DatabaseError('No messages found.')
    cursor.close()
    return last_message

def generate_asset_id (asset_name, block_index):
    if asset_name == config.BTC: return 0
    elif asset_name == config.XCP: return 1

    if len(asset_name) < 4:
        raise exceptions.AssetNameError('too short')

    # Numeric asset names.
    if enabled('numeric_asset_names', block_index):  # Protocol change.
        if asset_name[0] == 'A':
            # Must be numeric.
            try:
                asset_id = int(asset_name[1:])
            except ValueError:
                raise exceptions.AssetNameError('non‐numeric asset name starts with ‘A’')

            # Number must be in range.
            if not (26**12 + 1 <= asset_id <= 2**64 - 1):
                raise exceptions.AssetNameError('numeric asset name not in range')

            return asset_id
        elif len(asset_name) >= 13:
            raise exceptions.AssetNameError('long asset names must be numeric')

    if asset_name[0] == 'A': raise exceptions.AssetNameError('non‐numeric asset name starts with ‘A’')

    # Convert the Base 26 string to an integer.
    n = 0
    for c in asset_name:
        n *= 26
        if c not in b26_digits:
            raise exceptions.AssetNameError('invalid character:', c)
        digit = b26_digits.index(c)
        n += digit
    asset_id = n

    if asset_id < 26**3:
        raise exceptions.AssetNameError('too short')

    return asset_id

def generate_asset_name (asset_id, block_index):
    if asset_id == 0: return config.BTC
    elif asset_id == 1: return config.XCP

    if asset_id < 26**3:
        raise exceptions.AssetIDError('too low')

    if enabled('numeric_asset_names', block_index):  # Protocol change.
        if asset_id <= 2**64 - 1:
            if 26**12 + 1 <= asset_id:
                asset_name = 'A' + str(asset_id)
                return asset_name
        else:
            raise exceptions.AssetIDError('too high')

    # Divide that integer into Base 26 string.
    res = []
    n = asset_id
    while n > 0:
        n, r = divmod (n, 26)
        res.append(b26_digits[r])
    asset_name = ''.join(res[::-1])

    """
    return asset_name + checksum.compute(asset_name)
    """
    return asset_name


def get_asset_id (db, asset_name, block_index):
    if not enabled('hotfix_numeric_assets', block_index):
        return generate_asset_id(asset_name, block_index)
    cursor = db.cursor()
    cursor.execute('''SELECT * FROM assets WHERE asset_name = ?''', (asset_name,))
    assets = list(cursor)
    if len(assets) == 1:
        return int(assets[0]['asset_id'])
    else:
        raise exceptions.AssetError('No such asset: {}'.format(asset_name))

def get_asset_name (db, asset_id, block_index):
    if not enabled('hotfix_numeric_assets', block_index):
        return generate_asset_name(asset_id, block_index)
    cursor = db.cursor()
    cursor.execute('''SELECT * FROM assets WHERE asset_id = ?''', (str(asset_id),))
    assets = list(cursor)
    if len(assets) == 1:
        return assets[0]['asset_name']
    elif not assets:
        return 0    # Strange, I know…


class DebitError (Exception): pass
def debit (db, block_index, address, asset, quantity, action=None, event=None):
    if type(quantity) != int:
        raise DebitError
    if quantity < 0:
        raise DebitError
    if asset == config.BTC:
        raise DebitError

    debit_cursor = db.cursor()

    # Contracts can only hold XCP balances.
    if enabled('contracts_only_xcp_balances', block_index): # Protocol change.
        if len(address) == 40:
            assert asset == config.XCP

    if asset == config.BTC:
        raise exceptions.BalanceError('Cannot debit bitcoins from a {} address!'.format(config.XCP_NAME))

    debit_cursor.execute('''SELECT * FROM balances \
                            WHERE (address = ? AND asset = ?)''', (address, asset))
    balances = debit_cursor.fetchall()
    if not len(balances) == 1: old_balance = 0
    else: old_balance = balances[0]['quantity']

    if old_balance < quantity:
        raise DebitError('Insufficient funds.')

    balance = round(old_balance - quantity)
    balance = min(balance, config.MAX_INT)
    assert balance >= 0

    bindings = {
        'quantity': balance,
        'address': address,
        'asset': asset
    }
    sql='update balances set quantity = :quantity where (address = :address and asset = :asset)'
    debit_cursor.execute(sql, bindings)

    # Record debit.
    bindings = {
        'block_index': block_index,
        'address': address,
        'asset': asset,
        'quantity': quantity,
        'action': action,
        'event': event
    }
    sql='insert into debits values(:block_index, :address, :asset, :quantity, :action, :event)'
    debit_cursor.execute(sql, bindings)
    debit_cursor.close()

    BLOCK_LEDGER.append('{}{}{}{}'.format(block_index, address, asset, quantity))

class CreditError (Exception): pass
def credit (db, block_index, address, asset, quantity, action=None, event=None):
    if type(quantity) != int:
        raise CreditError
    if quantity < 0:
        raise CreditError
    if asset == config.BTC:
        raise CreditError

    credit_cursor = db.cursor()

    # Contracts can only hold XCP balances.
    if enabled('contracts_only_xcp_balances', block_index): # Protocol change.
        if len(address) == 40:
            assert asset == config.XCP

    credit_cursor.execute('''SELECT * FROM balances \
                             WHERE (address = ? AND asset = ?)''', (address, asset))
    balances = credit_cursor.fetchall()
    if len(balances) == 0:
        assert balances == []

        #update balances table with new balance
        bindings = {
            'address': address,
            'asset': asset,
            'quantity': quantity,
        }
        sql='insert into balances values(:address, :asset, :quantity)'
        credit_cursor.execute(sql, bindings)
    elif len(balances) > 1:
        assert False
    else:
        old_balance = balances[0]['quantity']
        assert type(old_balance) == int
        balance = round(old_balance + quantity)
        balance = min(balance, config.MAX_INT)

        bindings = {
            'quantity': balance,
            'address': address,
            'asset': asset
        }
        sql='update balances set quantity = :quantity where (address = :address and asset = :asset)'
        credit_cursor.execute(sql, bindings)

    # Record credit.
    bindings = {
        'block_index': block_index,
        'address': address,
        'asset': asset,
        'quantity': quantity,
        'action': action,
        'event': event
    }
    sql='insert into credits values(:block_index, :address, :asset, :quantity, :action, :event)'
    credit_cursor.execute(sql, bindings)
    credit_cursor.close()

    BLOCK_LEDGER.append('{}{}{}{}'.format(block_index, address, asset, quantity))

class QuantityError(Exception): pass

def is_divisible(db, asset):
    if asset in (config.BTC, config.XCP):
        return True
    else:
        cursor = db.cursor()
        cursor.execute('''SELECT * FROM issuances \
                          WHERE (status = ? AND asset = ?)''', ('valid', asset))
        issuances = cursor.fetchall()
        if not issuances: raise exceptions.AssetError('No such asset: {}'.format(asset))
        return issuances[0]['divisible']

def value_in (db, quantity, asset, divisible=None):

    if asset == 'leverage':
        return round(quantity)

    if asset in ('value', 'fraction', 'price', 'odds'):
        return float(quantity)  # TODO: Float?!

    if divisible == None:
        divisible = is_divisible(db, asset)

    if divisible:
        quantity = D(quantity) * config.UNIT
        if quantity == quantity.to_integral():
            return int(quantity)
        else:
            raise quantityerror('divisible assets have only eight decimal places of precision.')
    else:
        quantity = D(quantity)
        if quantity != round(quantity):
            raise QuantityError('Fractional quantities of indivisible assets.')
        return round(quantity)

def value_out (db, quantity, asset, divisible=None):

    def norm(num, places):
        # Round only if necessary.
        num = round(num, places)
        fmt = '{:.' + str(places) + 'f}'
        num = fmt.format(num)
        return num.rstrip('0')+'0' if num.rstrip('0')[-1] == '.' else num.rstrip('0')

    if asset == 'fraction':
        return str(norm(D(quantity) * D(100), 6)) + '%'

    if asset in ('leverage', 'value', 'price', 'odds'):
        return norm(quantity, 6)

    if divisible == None:
        divisible = is_divisible(db, asset)

    if divisible:
        quantity = D(quantity) / D(config.UNIT)
        if quantity == quantity.to_integral():
            return str(quantity) + '.0'  # For divisible assets, display the decimal point.
        else:
            return norm(quantity, 8)
    else:
        quantity = D(quantity)
        if quantity != round(quantity):
            raise QuantityError('Fractional quantities of indivisible assets.')
        return round(quantity)



### SUPPLIES ###

def holders(db, asset):
    holders = []
    cursor = db.cursor()
    # Balances
    cursor.execute('''SELECT * FROM balances \
                      WHERE asset = ?''', (asset,))
    for balance in list(cursor):
        holders.append({'address': balance['address'], 'address_quantity': balance['quantity'], 'escrow': None})
    # Funds escrowed in orders. (Protocol change.)
    cursor.execute('''SELECT * FROM orders \
                      WHERE give_asset = ? AND status = ?''', (asset, 'open'))
    for order in list(cursor):
        holders.append({'address': order['source'], 'address_quantity': order['give_remaining'], 'escrow': order['tx_hash']})
    # Funds escrowed in pending order matches. (Protocol change.)
    cursor.execute('''SELECT * FROM order_matches \
                      WHERE (forward_asset = ? AND status = ?)''', (asset, 'pending'))
    for order_match in list(cursor):
        holders.append({'address': order_match['tx0_address'], 'address_quantity': order_match['forward_quantity'], 'escrow': order_match['id']})
    cursor.execute('''SELECT * FROM order_matches \
                      WHERE (backward_asset = ? AND status = ?)''', (asset, 'pending'))
    for order_match in list(cursor):
        holders.append({'address': order_match['tx1_address'], 'address_quantity': order_match['backward_quantity'], 'escrow': order_match['id']})

    # Bets and RPS (and bet/rps matches) only escrow XCP.
    if asset == config.XCP:
        cursor.execute('''SELECT * FROM bets \
                          WHERE status = ?''', ('open',))
        for bet in list(cursor):
            holders.append({'address': bet['source'], 'address_quantity': bet['wager_remaining'], 'escrow': bet['tx_hash']})
        cursor.execute('''SELECT * FROM bet_matches \
                          WHERE status = ?''', ('pending',))
        for bet_match in list(cursor):
            holders.append({'address': bet_match['tx0_address'], 'address_quantity': bet_match['forward_quantity'], 'escrow': bet_match['id']})
            holders.append({'address': bet_match['tx1_address'], 'address_quantity': bet_match['backward_quantity'], 'escrow': bet_match['id']})

        cursor.execute('''SELECT * FROM rps \
                          WHERE status = ?''', ('open',))
        for rps in list(cursor):
            holders.append({'address': rps['source'], 'address_quantity': rps['wager'], 'escrow': rps['tx_hash']})
        cursor.execute('''SELECT * FROM rps_matches \
                          WHERE status IN (?, ?, ?)''', ('pending', 'pending and resolved', 'resolved and pending'))
        for rps_match in list(cursor):
            holders.append({'address': rps_match['tx0_address'], 'address_quantity': rps_match['wager'], 'escrow': rps_match['id']})
            holders.append({'address': rps_match['tx1_address'], 'address_quantity': rps_match['wager'], 'escrow': rps_match['id']})

        cursor.execute('''SELECT * FROM executions WHERE status = ?''', ('valid',))
        for execution in list(cursor):
            holders.append({'address': execution['source'], 'address_quantity': execution['gas_cost'], 'escrow': None})

    cursor.close()
    return holders

def xcp_created (db):
    cursor = db.cursor()
    cursor.execute('''SELECT * FROM burns \
                      WHERE (status = ?)''', ('valid',))
    total = sum([burn['earned'] for burn in list(cursor)])
    cursor.close()
    return total
def xcp_destroyed (db):
    cursor = db.cursor()
    # Destructions
    cursor.execute('''SELECT * FROM destructions \
                      WHERE (status = ? AND asset = ?)''', ('valid', config.XCP))
    destroyed_total = sum([destruction['quantity'] for destruction in list(cursor)])
    # Subtract issuance fees.
    cursor.execute('''SELECT * FROM issuances\
                      WHERE status = ?''', ('valid',))
    issuance_fee_total = sum([issuance['fee_paid'] for issuance in cursor.fetchall()])
    # Subtract dividend fees.
    cursor.execute('''SELECT * FROM dividends\
                      WHERE status = ?''', ('valid',))
    dividend_fee_total = sum([dividend['fee_paid'] for dividend in cursor.fetchall()])
    cursor.close()
    return destroyed_total + issuance_fee_total + dividend_fee_total
def xcp_supply (db):
    return xcp_created(db) - xcp_destroyed(db)
def creations (db):
    cursor = db.cursor()
    creations = {config.XCP: xcp_created(db)}
    cursor.execute('''SELECT * from issuances \
                      WHERE status = ?''', ('valid',))
    for issuance in list(cursor):
        asset = issuance['asset']
        quantity = issuance['quantity']
        if asset in creations.keys():
            creations[asset] += quantity
        else:
            creations[asset] = quantity

    cursor.close()
    return creations
def destructions (db):
    cursor = db.cursor()
    destructions = {config.XCP: xcp_destroyed(db)}
    cursor.execute('''SELECT * from destructions \
                      WHERE (status = ? AND asset != ?)''', ('valid', config.XCP))
    for destruction in list(cursor):
        asset = destruction['asset']
        quantity = destruction['burned']
        if asset in destructions.keys():
            destructions[asset] += quantity
        else:
            destructions[asset] = quantity
    cursor.close()
    return destructions
def asset_supply (db, asset):
    supply = creations(db)[asset]
    destroyed = destructions(db)
    if asset in destroyed:
        supply -= destroyed[asset]
    return supply
def supplies (db):
    d1 = creations(db)
    d2 = destructions(db)
    return {key: d1[key] - d2.get(key, 0) for key in d1.keys()}

### SUPPLIES ###


class GetURLError (Exception): pass
def get_url(url, abort_on_error=False, is_json=True, fetch_timeout=5):
    try:
        r = requests.get(url, timeout=fetch_timeout)
    except Exception as e:
        raise GetURLError("Got get_url request error: %s" % e)
    else:
        if r.status_code != 200 and abort_on_error:
            raise GetURLError("Bad status code returned: '%s'. result body: '%s'." % (r.status_code, r.text))
        result = json.loads(r.text) if is_json else r.text
    return result

def dhash_string(text):
    return binascii.hexlify(hashlib.sha256(hashlib.sha256(bytes(text, 'utf-8')).digest()).digest()).decode()


### Bitcoin Addresses ###

def validate_address(address):

    # Get array of pubkeyhashes to check.
    if is_multisig(address):
        pubkeyhashes = pubkeyhash_array(address)
    else:
        pubkeyhashes = [address]

    # Check validity by attempting to decode.
    for pubkeyhashes in pubkeyhashes:
        base58_check_decode(pubkeyhashes, config.ADDRESSVERSION)

def base58_encode(binary):
    # Convert big‐endian bytes to integer
    n = int('0x0' + binascii.hexlify(binary).decode('utf8'), 16)

    # Divide that integer into base58
    res = []
    while n > 0:
        n, r = divmod (n, 58)
        res.append(b58_digits[r])
    res = ''.join(res[::-1])

    return res

def base58_check_encode(original, version):
    b = binascii.unhexlify(bytes(original, 'utf-8'))
    d = version + b

    binary = d + dhash(d)[:4]
    res = base58_encode(binary)

    # Encode leading zeros as base58 zeros
    czero = 0
    pad = 0
    for c in d:
        if c == czero: pad += 1
        else: break

    address = b58_digits[0] * pad + res

    if bytes(original, 'utf-8') != binascii.hexlify(base58_check_decode(address, version)):
        raise exceptions.AddressError('encoded address does not decode properly')

    return address

def base58_check_decode (s, version):
    # Convert the string to an integer
    n = 0
    for c in s:
        n *= 58
        if c not in b58_digits:
            raise exceptions.InvalidBase58Error('Not a valid base58 character:', c)
        digit = b58_digits.index(c)
        n += digit

    # Convert the integer to bytes
    h = '%x' % n
    if len(h) % 2:
        h = '0' + h
    res = binascii.unhexlify(h.encode('utf8'))

    # Add padding back.
    pad = 0
    for c in s[:-1]:
        if c == b58_digits[0]: pad += 1
        else: break
    k = version * pad + res

    addrbyte, data, chk0 = k[0:1], k[1:-4], k[-4:]
    if addrbyte != version:
        raise exceptions.VersionByteError('incorrect version byte')
    chk1 = dhash(addrbyte + data)[:4]
    if chk0 != chk1:
        raise exceptions.Base58ChecksumError('Checksum mismatch: %r ≠ %r' % (chk0, chk1))
    return data

### Bitcoin Addresses ###


### Multi‐signature Addresses ###
# NOTE: a `pub` is either a pubkey or a pubkeyhash

class MultiSigAddressError (exceptions.AddressError):
    pass

def is_multisig(address):
    array = address.split('_')
    return (len(array) > 1)

def canonical_address(address):
    if is_multisig(address):
        signatures_required, pubkeyhashes, signatures_possible = extract_array(address)
        if not all([base58_check_decode(pubkeyhash, config.ADDRESSVERSION) for pubkeyhash in pubkeyhashes]):
            raise MultiSigAddressError('Multi‐signature address must use PubKeyHashes, not public keys.')
        return construct_array(signatures_required, pubkeyhashes, signatures_possible)
    else:
        return address
def test_array(signatures_required, pubs, signatures_possible):
    try:
        signatures_required, signatures_possible = int(signatures_required), int(signatures_possible)
    except ValueError:
        raise MultiSigAddressError('Signature values not integers.')
    if signatures_required < 1 or signatures_required > 3:
        raise MultiSigAddressError('Invalid signatures_required.')
    if signatures_possible < 2 or signatures_possible > 3:
        raise MultiSigAddressError('Invalid signatures_possible.')
    if signatures_possible != len(pubs):
        raise exceptions.InputError('Incorrect number of pubkeys/pubkeyhashes in multi‐signature address.')

def construct_array(signatures_required, pubs, signatures_possible):
    test_array(signatures_required, pubs, signatures_possible)
    address = '_'.join([str(signatures_required)] + sorted(pubs) + [str(signatures_possible)])
    return address

def extract_array(address):
    assert is_multisig(address)
    array = address.split('_')
    signatures_required, pubs, signatures_possible = array[0], sorted(array[1:-1]), array[-1]
    test_array(signatures_required, pubs, signatures_possible)
    return int(signatures_required), pubs, int(signatures_possible)

def pubkeyhash_array(address):
    signatures_required, pubkeyhashes, signatures_possible = extract_array(address)
    if not all([base58_check_decode(pubkeyhash, config.ADDRESSVERSION) for pubkeyhash in pubkeyhashes]):
        raise MultiSigAddressError('Multi‐signature address must use PubKeyHashes, not public keys.')
    return pubkeyhashes

### Multi‐signature Addresses ###

def get_balance (db, address, asset):
    # Get balance of contract or address.
    cursor = db.cursor()
    balances = list(cursor.execute('''SELECT * FROM balances WHERE (address = ? AND asset = ?)''', (address, asset)))
    cursor.close()
    if not balances: return 0
    else: return balances[0]['quantity']

# Why on Earth does `binascii.hexlify()` return bytes?!
def hexlify(x):
    return binascii.hexlify(x).decode('ascii')

### Protocol Changes ###
def enabled (change_name, block_index):
    enable_block_index = PROTOCOL_CHANGES[change_name]['block_index']

    if config.TESTNET: 
        return True     # Protocol changes are always retroactive on testnet.
    else:
        if block_index >= enable_block_index:
            return True
        else:
            return False
    assert False

### Unconfirmed Transactions ###

# TODO: use scriptpubkey_to_address()
@lru_cache(maxsize=4096)
def extract_addresses(tx):
    tx = json.loads(tx) # for lru_cache
    addresses = []

    for vout in tx['vout']:
        if 'addresses' in vout['scriptPubKey']:
            addresses += vout['scriptPubKey']['addresses']

    for vin in tx['vin']:
        vin_tx = backend.get_cached_raw_transaction(vin['txid'], verbose=True)
        vout = vin_tx['vout'][vin['vout']]
        if 'addresses' in vout['scriptPubKey']:
            addresses += vout['scriptPubKey']['addresses']

    return addresses

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, D):
            return format(obj, '.8f')
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)

def unconfirmed_transactions(address):
    unconfirmed_tx = []

    call_id = 0
    call_list = []
    for tx_hash in backend.old_rpc('getrawmempool', []):
        call_list.append({
            "method": 'getrawtransaction',
            "params": [tx_hash, 1],
            "jsonrpc": "2.0",
            "id": call_id
        })
        call_id += 1

    batch_responses = backend.get_proxy()._batch(call_list)
    for response in batch_responses:
        if 'error' not in response or response['error'] is None:
            if 'result' in response and response['result'] is not None:
                tx = response['result']
                addresses = extract_addresses(json.dumps(tx, cls=DecimalEncoder))
                if address in addresses:
                    unconfirmed_tx.append(tx)

    return unconfirmed_tx


def transfer(db, block_index, source, destination, asset, quantity, action, event):
    debit(db, block_index, source, asset, quantity, action=action, event=event)
    credit(db, block_index, destination, asset, quantity, action=action, event=event)

def get_balance (db, address, asset):
    # Get balance of contract or address.
    cursor = db.cursor()
    balances = list(cursor.execute('''SELECT * FROM balances WHERE (address = ? AND asset = ?)''', (address, asset)))
    cursor.close()
    if not balances: return 0
    else: return balances[0]['quantity']

ID_SEPARATOR = '_'
def make_id(hash_1, hash_2):
    return hash_1 + ID_SEPARATOR + hash_2
def parse_id(match_id):
    assert match_id[64] == ID_SEPARATOR
    return match_id[:64], match_id[65:] # UTF-8 encoding means that the indices are doubled.

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
