import calendar
import datetime
from decimal import Decimal
import time
import itertools
from cryptokit.base58 import address_version
from sqlalchemy.exc import SQLAlchemyError
import yaml

from flask import current_app, session
from bitcoinrpc import CoinRPCException

from . import db, cache, root, redis_conn
from .models import (OneMinuteReject, OneMinuteShare,
                     FiveMinuteShare, FiveMinuteReject, Payout, Block,
                     OneHourShare, OneHourReject, UserSettings)


class CommandException(Exception):
    pass


class CurrencyKeeper(dict):
    __getattr__ = dict.__getitem__

    def __init__(self, *args, **kwargs):
        super(CurrencyKeeper, self).__init__(*args, **kwargs)
        self.version_lut = {}

    def setcurr(self, val):
        setattr(self, val.currency_name, val)
        self.__setitem__(val.currency_name, val)
        for ver in val.address_version:
            self.version_lut[ver] = val

    def payout_currencies(self):
        return [c for c in self.itervalues() if getattr(c, 'exchangeable', False)]

    def lookup_address(self, address):
        ver = address_version(address)
        try:
            return self.lookup_version(ver)
        except AttributeError:
            raise AttributeError("Address '{}' version {} is not a configured currency. Options are {}"
                                 .format(address, ver, self.available_versions))

    @property
    def available_versions(self):
        return {k: v.currency_name for k, v in self.version_lut.iteritems()}

    def lookup_version(self, version):
        try:
            return self.version_lut[version]
        except KeyError:
            raise AttributeError(
                "Address version {} doesn't match available versions {}"
                .format(version, self.available_versions))


def timeit(method):
    def timed(*args, **kw):
        ts = time.time()
        result = method(*args, **kw)
        te = time.time()

        current_app.logger.info('{} (args {}, kwargs {}) in {}'
                                .format(method.__name__,
                                        args, kw, time_format(te - ts)))
        return result

    return timed


@cache.memoize(timeout=3600)
def get_pool_acc_rej(timedelta=None):
    """ Get accepted and rejected share count totals for the last month """
    if timedelta is None:
        timedelta = datetime.timedelta(days=30)

    # Pull from five minute shares if we're looking at a day timespan
    if timedelta <= datetime.timedelta(days=1):
        rej_typ = FiveMinuteReject
        acc_typ = FiveMinuteShare
    else:
        rej_typ = OneHourReject
        acc_typ = OneHourShare

    one_month_ago = datetime.datetime.utcnow() - timedelta
    rejects = (rej_typ.query.
               filter(rej_typ.time >= one_month_ago).
               filter_by(user="pool_stale"))
    accepts = (acc_typ.query.
               filter(acc_typ.time >= one_month_ago).
               filter_by(user="pool"))
    reject_total = sum([hour.value for hour in rejects])
    accept_total = sum([hour.value for hour in accepts])
    return reject_total, accept_total


@cache.memoize(timeout=3600)
def users_blocks(address, algo=None, merged=None):
    q = db.session.query(Block).filter_by(user=address, merged_type=None)
    if algo:
        q.filter_by(algo=algo)
    return algo.count()


@cache.memoize(timeout=86400)
def all_time_shares(address):
    shares = db.session.query(OneHourShare).filter_by(user=address)
    return sum([share.value for share in shares])


@cache.memoize(timeout=60)
def last_block_time(algo, merged_type=None):
    return last_block_time_nocache(algo, merged_type=merged_type)


def last_block_time_nocache(algo, merged_type=None):
    """ Retrieves the last time a block was solved using progressively less
    accurate methods. Essentially used to calculate round time.
    TODO XXX: Add pool selector to each of the share queries to grab only x11,
    etc
    """
    last_block = Block.query.filter_by(merged_type=merged_type, algo=algo).order_by(Block.height.desc()).first()
    if last_block:
        return last_block.found_at

    hour = OneHourShare.query.order_by(OneHourShare.time).first()
    if hour:
        return hour.time

    five = FiveMinuteShare.query.order_by(FiveMinuteShare.time).first()
    if five:
        return five.time

    minute = OneMinuteShare.query.order_by(OneMinuteShare.time).first()
    if minute:
        return minute.time

    return datetime.datetime.utcnow()


@cache.memoize(timeout=60)
def last_block_share_id(currency, merged_type=None):
    return last_block_share_id_nocache(currency, merged_type=merged_type)


def last_block_share_id_nocache(algorithm=None, merged_type=None):
    last_block = Block.query.filter_by(merged_type=merged_type).order_by(Block.height.desc()).first()
    if not last_block or not last_block.last_share_id:
        return 0
    return last_block.last_share_id


@cache.memoize(timeout=60)
def last_block_found(algorithm=None, merged_type=None):
    last_block = Block.query.filter_by(merged_type=merged_type).order_by(Block.height.desc()).first()
    if not last_block:
        return None
    return last_block


def last_blockheight(merged_type=None):
    last = last_block_found(merged_type=merged_type)
    if not last:
        return 0
    return last.height


def get_typ(typ, address=None, window=True, worker=None, q_typ=None):
    """ Gets the latest slices of a specific size. window open toggles
    whether we limit the query to the window size or not. We disable the
    window when compressing smaller time slices because if the crontab
    doesn't run we don't want a gap in the graph. This is caused by a
    portion of data that should already be compressed not yet being
    compressed. """
    # grab the correctly sized slices
    base = db.session.query(typ)

    if address is not None:
        base = base.filter_by(user=address)
    if worker is not None:
        base = base.filter_by(worker=worker)
    if q_typ is not None:
        base = base.filter_by(typ=q_typ)
    if window is False:
        return base
    grab = typ.floor_time(datetime.datetime.utcnow()) - typ.window
    return base.filter(typ.time >= grab)


def compress_typ(typ, workers, address=None, worker=None):
    for slc in get_typ(typ, address, window=False, worker=worker):
        if worker is not None:
            slice_dt = typ.floor_time(slc.time)
            stamp = calendar.timegm(slice_dt.utctimetuple())
            workers.setdefault(slc.device, {})
            workers[slc.device].setdefault(stamp, 0)
            workers[slc.device][stamp] += slc.value
        else:
            slice_dt = typ.upper.floor_time(slc.time)
            stamp = calendar.timegm(slice_dt.utctimetuple())
            workers.setdefault(slc.worker, {})
            workers[slc.worker].setdefault(stamp, 0)
            workers[slc.worker][stamp] += slc.value


@cache.cached(timeout=60, key_prefix='pool_hashrate')
def get_pool_hashrate(algo):
    """ Retrieves the pools hashrate average for the last 10 minutes. """
    dt = datetime.datetime.utcnow()
    twelve_ago = dt - datetime.timedelta(minutes=12)
    two_ago = dt - datetime.timedelta(minutes=2)
    ten_min = (OneMinuteShare.query.filter_by(user='pool')
               .filter(OneMinuteShare.time >= twelve_ago, OneMinuteShare.time <= two_ago))
    ten_min = sum([min.value for min in ten_min])
    # shares times hashes per n1 share divided by 600 seconds and 1000 to get
    # khash per second
    return float(ten_min) / 600000


@cache.memoize(timeout=30)
def get_round_shares(algo, merged_type=None):
    """ Retrieves the total shares that have been submitted since the last
    round rollover. """
    suffix = algo
    if merged_type:
        suffix += "_" + merged_type
    return sum(redis_conn.hvals('current_block_' + suffix)), datetime.datetime.utcnow()


def get_adj_round_shares(khashrate):
    """ Since round shares are cached we still want them to update on every
    page reload, so we extrapolate a new value based on computed average
    shares per second for the round, then add that for the time since we
    computed the real value. """
    round_shares, dt = get_round_shares()
    # # compute average shares/second
    now = datetime.datetime.utcnow()
    sps = float(khashrate * 1000)
    round_shares += int(round((now - dt).total_seconds() * sps))
    return round_shares, sps


@cache.cached(timeout=60, key_prefix='alerts')
def get_alerts():
    return yaml.load(open(root + '/static/yaml/alerts.yaml'))


@cache.memoize(timeout=60)
def last_10_shares(user):
    twelve_ago = datetime.datetime.utcnow() - datetime.timedelta(minutes=12)
    two_ago = datetime.datetime.utcnow() - datetime.timedelta(minutes=2)
    minutes = (OneMinuteShare.query.
               filter_by(user=user).filter(OneMinuteShare.time > twelve_ago, OneMinuteShare.time < two_ago))
    if minutes:
        return sum([min.value for min in minutes])
    return 0


def collect_acct_items(address, limit=None, offset=0):
    """ Get account items for a specific user """
    payouts = (Payout.query.filter_by(user=address).join(Payout.block).
               order_by(Block.found_at.desc()).limit(limit).offset(offset))
    return payouts


def collect_user_stats(address):
    """ Accumulates all aggregate user data for serving via API or rendering
    into main user stats page """
    # store all the raw data of we're gonna grab
    workers = {}
    # blank worker template
    def_worker = {'accepted': 0, 'rejected': 0, 'last_10_shares': 0,
                  'online': False, 'server': {}}
    # for picking out the last 10 minutes worth shares...
    now = datetime.datetime.utcnow().replace(second=0, microsecond=0)
    twelve_ago = now - datetime.timedelta(minutes=12)
    two_ago = now - datetime.timedelta(minutes=2)
    for m in itertools.chain(get_typ(FiveMinuteShare, address),
                             get_typ(OneMinuteShare, address)):
        workers.setdefault(m.worker, def_worker.copy())
        workers[m.worker]['accepted'] += m.value
        # if in the right 10 minute window add to list
        if m.time >= twelve_ago and m.time < two_ago:
            workers[m.worker]['last_10_shares'] += m.value

    # accumulate reject amount
    for m in itertools.chain(get_typ(FiveMinuteReject, address),
                             get_typ(OneMinuteReject, address)):
        workers.setdefault(m.worker, def_worker.copy())
        workers[m.worker]['rejected'] += m.value

    # pull online status from cached pull direct from powerpool servers
    for name, host in cache.get('addr_online_' + address) or []:
        workers.setdefault(name, def_worker.copy())
        workers[name]['online'] = True
        try:
            workers[name]['server'] = current_app.config['monitor_addrs'][host]['stratum']
        except KeyError:
            workers[name]['server'] = ''

    # pre-calculate a few of the values here to abstract view logic
    for name, w in workers.iteritems():
        workers[name]['last_10_hashrate'] = (shares_to_hashes(w['last_10_shares']) / 1000000) / 600
        if w['accepted'] or w['rejected']:
            workers[name]['efficiency'] = 100.0 * (float(w['accepted']) / (w['accepted'] + w['rejected']))
        else:
            workers[name]['efficiency'] = None

    # sort the workers by their name
    new_workers = []
    for name, data in workers.iteritems():
        new_workers.append(data)
        new_workers[-1]['name'] = name
    new_workers = sorted(new_workers, key=lambda x: x['name'])

    # show their donation percentage
    user = UserSettings.query.filter_by(user=address).first()
    if not user:
        perc = 0
    else:
        perc = user.hr_perc

    user_last_10_shares = last_10_shares(address)
    last_10_hashrate = (shares_to_hashes(user_last_10_shares) / 1000000) / 600
    now = datetime.datetime.now()
    next_exchange = now.replace(minute=0, second=0, microsecond=0, hour=((now.hour + 2) % 23))
    next_payout = now.replace(minute=0, second=0, microsecond=0, hour=0)

    f_perc = Decimal(current_app.config.get('fee_perc', Decimal('0.02'))) * 100

    return dict(workers=new_workers,
                acct_items=collect_acct_items(address, 20),
                donation_perc=perc,
                last_10_shares=user_last_10_shares,
                last_10_hashrate=last_10_hashrate,
                next_payout=next_payout,
                next_exchange=next_exchange,
                f_per=f_perc)


def get_pool_eff(timedelta=None):
    rej, acc = get_pool_acc_rej(timedelta)
    # avoid zero division error
    if not rej and not acc:
        return 100
    else:
        return (float(acc) / (acc + rej)) * 100


def shares_to_hashes(shares):
    return float(current_app.config.get('hashes_per_share', 65536)) * shares


def resort_recent_visit(recent):
    """ Accepts a new dictionary of recent visitors and calculates what
    percentage of your total visits have gone to that address. Used to dim low
    percentage addresses. Also sortes showing most visited on top. """
    # accumulate most visited addr while trimming dictionary. NOT Python3 compat
    session['recent_users'] = []
    for i, (addr, visits) in enumerate(sorted(recent.items(), key=lambda x: x[1], reverse=True)):
        if i > 20:
            del recent[addr]
            continue
        session['recent_users'].append((addr, visits))

    # total visits in the list, for calculating percentage
    total = float(sum([t[1] for t in session['recent_users']]))
    session['recent_users'] = [(addr, (visits / total))
                               for addr, visits in session['recent_users']]


class Benchmark(object):
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.start = time.time()

    def __exit__(self, ty, val, tb):
        end = time.time()
        current_app.logger.info("BENCHMARK: {} in {}"
                                .format(self.name, time_format(end - self.start)))
        return False


def time_format(seconds):
    # microseconds
    if seconds <= 1.0e-3:
        return "{:,.4f} us".format(seconds * 1000000.0)
    if seconds <= 1.0:
        return "{:,.4f} ms".format(seconds * 1000.0)
    return "{:,.4f} sec".format(seconds)


##############################################################################
# Message validation and verification functions
##############################################################################
def validate_message_vals(**kwargs):
    set_addrs = kwargs['SETADDR']
    del_addrs = kwargs['DELADDR']
    donate_perc = kwargs['SETDONATE']
    anon = kwargs['MAKEANON']

    for curr, addr in set_addrs.iteritems():
        curr = check_valid_currency(addr)
        if len(addr) != 34 or curr is False:
            raise CommandException("Invalid currency address passed!")

    try:
        donate_perc = Decimal(donate_perc).quantize(Decimal('0.01')) / 100
    except TypeError:
        raise CommandException("Donate percentage unable to be converted to python Decimal!")
    else:
        if donate_perc > 100.0 or donate_perc < 0:
            raise CommandException("Donate percentage was out of bounds!")


    return set_addrs, del_addrs, donate_perc, anon


def verify_message(address, curr, message, signature):
    update_dict = {'SETADDR': {}, 'DELADDR': [], 'MAKEANON': False,
                   'SETDONATE': 0}
    stamp = False
    site = False
    try:
        lines = message.split("\t")
        for line in lines:
            parts = line.split(" ")
            if parts[0] in update_dict:
                if parts[0] == 'SETADDR':
                    update_dict.setdefault(parts[0], {})
                    update_dict[parts[0]][parts[1]] = parts[2]
                elif parts[0] == 'DELADDR':
                    update_dict[parts[0]].append(parts[1])
                else:
                    update_dict[parts[0]] = parts[1]
            elif parts[0] == 'Only':
                site = parts[3]
            elif parts[0] == 'Generated':
                time = parts[2] + ' ' + parts[3] + ' ' + parts[4]
                stamp = datetime.datetime.strptime(time, '%Y-%m-%d %H:%M:%S.%f %Z')
            else:
                raise CommandException("Invalid command given! Generate a new "
                                       "message & try again.")
    except (IndexError, ValueError):
        current_app.logger.info("Invalid message provided", exc_info=True)
        raise CommandException("Invalid information provided in the message "
                               "field. This could be the fault of the bug with "
                               "IE11, or the generated message has an error")
    if not stamp:
        raise CommandException("Time stamp not found in message! Generate a new"
                               " message & try again.")

    now = datetime.datetime.utcnow()
    if abs((now - stamp).seconds) > current_app.config.get('message_expiry', 90660):
        raise CommandException("Signature/Message is too old to be accepted! "
                               "Generate a new message & try again.")

    if not site or site != current_app.config['site_title']:
        raise CommandException("Invalid website! Generate a new message "
                               "& try again.")

    current_app.logger.info(u"Attempting to validate message '{}' with sig '{}' for address '{}'"
                            .format(message, signature, address))

    try:
        res = curr.coinserv.verifymessage(address, signature, message.encode('utf-8').decode('unicode-escape'))
    except CoinRPCException as e:
        raise CommandException("Rejected by RPC server for reason {}!"
                               .format(e))
    except Exception:
        current_app.logger.error("Coinserver verification error!", exc_info=True)
        raise CommandException("Unable to communicate with coinserver!")

    if res:
        args = validate_message_vals(**update_dict)
        try:
            UserSettings.update(address, *args)
        except SQLAlchemyError:
            db.session.rollback()
            raise CommandException("Error saving new settings to the database!")
        else:
            db.session.commit()
    else:
        raise CommandException("Invalid signature! Coinserver returned {}"
                               .format(res))
