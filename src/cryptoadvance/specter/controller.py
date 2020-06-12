import ast, sys, json, os, time, base64
import requests
import random, copy
from collections import OrderedDict
from threading import Thread
from .devices.key import Key


from functools import wraps
from flask import g, request, redirect, url_for

from flask import Flask, Blueprint, render_template, request, redirect, url_for, jsonify, flash
from flask_login import login_required, login_user, logout_user, current_user
from flask_login.config import EXEMPT_METHODS


from .helpers import normalize_xpubs, run_shell, set_loglevel, get_loglevel
from .descriptor import AddChecksum

from .specter import Specter
from .specter_error import SpecterError
from .wallet_manager import purposes
from .rpc import RpcError
from datetime import datetime
import urllib

from pathlib import Path
env_path = Path('.') / '.flaskenv'
from dotenv import load_dotenv
load_dotenv(env_path)

from flask import current_app as app
rand = random.randint(0, 1e32) # to force style refresh

########## template injections #############
@app.context_processor
def inject_debug():
    ''' Can be used in all jinja2 templates '''
    return dict(debug=app.config['DEBUG'])

################ routes ####################
@app.route('/wallets/<wallet_alias>/combine/', methods=['GET', 'POST'])
@login_required
def combine(wallet_alias):
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while combine: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    if request.method == 'POST': # FIXME: ugly...
        psbt0 = request.form.get('psbt0') # request.args.get('psbt0')
        psbt1 = request.form.get('psbt1') # request.args.get('psbt1')
        txid = request.form.get('txid')

        try:
            psbt = app.specter.combine([psbt0, psbt1])
            raw = app.specter.finalize(psbt)
        except RpcError as e:
            return e.error_msg, e.status_code
        except Exception as e:
            return "Unknown error: %r" % e, 500
        device_name = request.form.get('device_name')
        wallet.update_pending_psbt(psbt, txid, raw, device_name)
        return json.dumps(raw)
    return 'meh'

@app.route('/wallets/<wallet_alias>/broadcast/', methods=['GET', 'POST'])
@login_required
def broadcast(wallet_alias):
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while broadcast: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    if request.method == 'POST':
        tx = request.form.get('tx')
        if wallet.cli.testmempoolaccept([tx])[0]['allowed']:
            app.specter.broadcast(tx)
            wallet.delete_pending_psbt(wallet.cli.decoderawtransaction(tx)['txid'])
            return jsonify(success=True)
        else:
            return jsonify(success=False, error="Failed to broadcast transaction: transaction is invalid")
    return jsonify(success=False, error="broadcast tx request must use POST")

@app.route('/')
@login_required
def index():
    app.specter.check()
    if len(app.specter.wallet_manager.wallets) > 0:
        return redirect("/wallets/%s" % app.specter.wallet_manager.wallets[app.specter.wallet_manager.wallets_names[0]]["alias"])

    # TODO: add onboarding process
    if len(app.specter.device_manager.devices) == 0:
        # For now: can't do anything until a device is registered
        return redirect("/new_device/")

    return render_template("base.jinja", specter=app.specter, rand=rand)

@app.route('/login', methods=['GET', 'POST'])
def login():
    ''' login '''
    app.specter.check()
    if request.method == 'POST': 
        # ToDo: check the password via RPC-call
        if app.specter.cli is None:
            flash("We could not check your password, maybe Bitcoin Core is not running or not configured?","error")
            app.logger.info("AUDIT: Failed to check password")
            return render_template('login.jinja', specter=app.specter, data={'controller':'controller.login'}), 401
        cli = app.specter.cli.clone()
        cli.passwd = request.form['password']
        if cli.test_connection():
            app.login()
            app.logger.info("AUDIT: Successfull Login via RPC-credentials")
            flash('Logged in successfully.',"info")
            if request.form.get('next') and request.form.get('next').startswith("http"):
                response = redirect(request.form['next'])
            else:
                response = redirect(url_for('index'))
            return response
        else:
            flash('Invalid username or password', "error")
            app.logger.info("AUDIT: Invalid password login attempt")
            return render_template('login.jinja', specter=app.specter, data={'controller':'controller.login'}), 401
    else:
        if app.config.get('LOGIN_DISABLED'):
            return redirect('/')
        return render_template('login.jinja', specter=app.specter, data={'next':request.args.get('next')})

@app.route('/logout', methods=['GET', 'POST'])
def logout():
    logout_user()
    flash('You were logged out',"info")
    return redirect("/login") 

@app.route('/settings/', methods=['GET', 'POST'])
@login_required
def settings():
    app.specter.check()
    rpc = app.specter.config['rpc']
    user = rpc['user']
    passwd = rpc['password']
    port = rpc['port']
    host = rpc['host']
    protocol = 'http'
    explorer = app.specter.explorer
    auth = app.specter.config["auth"]
    hwi_bridge_url = app.specter.config['hwi_bridge_url']
    loglevel = get_loglevel(app)
    if "protocol" in rpc:
        protocol = rpc["protocol"]
    test = None
    if request.method == 'POST':
        user = request.form['username']
        passwd = request.form['password']
        port = request.form['port']
        host = request.form['host']
        explorer = request.form["explorer"]
        auth = request.form['auth']
        loglevel = request.form["loglevel"]
        hwi_bridge_url = request.form['hwi_bridge_url']
        action = request.form['action']
        # protocol://host
        if "://" in host:
            arr = host.split("://")
            protocol = arr[0]
            host = arr[1]

        if action == "test":
            test = app.specter.test_rpc(user=user,
                                        password=passwd,
                                        port=port,
                                        host=host,
                                        protocol=protocol,
                                        autodetect=False
                                        )
        if action == "save":
            app.specter.update_rpc( user=user,
                                    password=passwd,
                                    port=port,
                                    host=host,
                                    protocol=protocol,
                                    autodetect=False
                                    )
            app.specter.update_explorer(explorer)
            app.specter.update_auth(auth)
            app.specter.update_hwi_bridge_url(hwi_bridge_url)
            if auth == "rpcpasswordaspin":
                app.config['LOGIN_DISABLED'] = False
            else:
                app.config['LOGIN_DISABLED'] = True
            set_loglevel(app,loglevel)
            app.specter.check()
            return redirect("/")
    else:
        pass
    return render_template("settings.jinja",
                            test=test,
                            username=user,
                            password=passwd,
                            port=port,
                            host=host,
                            protocol=protocol,
                            explorer=explorer,
                            auth=auth,
                            hwi_bridge_url=hwi_bridge_url,
                            loglevel=loglevel,
                            specter=app.specter,
                            rand=rand)

################# wallet management #####################

@app.route('/new_wallet/')
@login_required
def new_wallet():
    app.specter.check()
    err = None
    if app.specter.chain is None:
        err = "Configure Bitcoin Core to create wallets"
        return render_template("base.jinja", error=err, specter=app.specter, rand=rand)
    return render_template("wallet/new_wallet/new_wallet_type.jinja", specter=app.specter, rand=rand)

@app.route('/new_wallet/simple/', methods=['GET', 'POST'])
@login_required
def new_wallet_simple():
    app.specter.check()
    name = "Simple"
    wallet_name = name
    i = 2
    err = None
    while wallet_name in app.specter.wallet_manager.wallets_names:
        wallet_name = "%s %d" % (name, i)
        i += 1
    device = None
    if request.method == 'POST':
        action = request.form['action']
        wallet_name = request.form['wallet_name']
        if wallet_name in app.specter.wallet_manager.wallets_names:
            err = "Wallet already exists"
        if "device" not in request.form:
            err = "Select the device"
        else:
            device_name = request.form['device']
        wallet_type = request.form['type']
        if action == 'device' and err is None:
            device = copy.deepcopy(app.specter.device_manager.devices[device_name])
            prefix = "tpub"
            if app.specter.chain == "main":
                prefix = "xpub"
            allowed_types = ['', wallet_type]
            device.keys = [key for key in device.keys if key.xpub.startswith(prefix) and key.key_type in allowed_types]
            pur = {
                '': "General",
                "wpkh": "Segwit (bech32)",
                "sh-wpkh": "Nested Segwit",
                "pkh": "Legacy",
            }
            return render_template("wallet/new_wallet/new_wallet_keys.jinja", purposes=pur, wallet_type=wallet_type, wallet_name=wallet_name, device=device, error=err, specter=app.specter, rand=rand)
        if action == 'key' and err is None:
            original_xpub = request.form['key']
            device = app.specter.device_manager.devices[device_name]
            key = None
            for k in device.keys:
                if k.original == original_xpub:
                    key = k
                    break
            if key is None:
                return render_template("base.jinja", error="Key not found", specter=app.specter, rand=rand)
            # create a wallet here
            wallet = app.specter.wallet_manager.create_simple(wallet_name, wallet_type, key, device)
            app.logger.info("Created Wallet %s" % wallet_name)
            rescan_blockchain = 'rescanblockchain' in request.form
            if rescan_blockchain:
                app.logger.info("Rescanning Blockchain ...")
                if app.specter.info['chain'] == "main":
                    if not app.specter.info['pruned'] or app.specter.info['pruneheight'] < 481824:
                        startblock = 481824
                    else:
                        startblock = app.specter.info['pruneheight']
                else:
                    if not app.specter.info['pruned']:
                        startblock = 0
                    else:
                        startblock = app.specter.info['pruneheight']
                try:
                    wallet.cli.rescanblockchain(startblock, timeout=1)
                except requests.exceptions.ReadTimeout:
                    # this is normal behaviour in our usecase
                    pass
                except Exception as e:
                    app.logger.error("Exception while rescanning blockchain: %e" % e)
                    err = "%r" % e
                wallet.getdata()
            return redirect("/wallets/%s/" % wallet["alias"])
    return render_template("wallet/new_wallet/new_wallet.jinja", wallet_name=wallet_name, device=device, error=err, specter=app.specter, rand=rand)

@app.route('/new_wallet/multisig/', methods=['GET', 'POST'])
@login_required
def new_wallet_multi():
    app.specter.check()
    name = "Multisig"
    wallet_type = "wsh"
    wallet_name = name
    i = 2
    err = None
    while wallet_name in app.specter.wallet_manager.wallets_names:
        wallet_name = "%s %d" % (name, i)
        i+=1

    sigs_total = len(app.specter.device_manager.devices)
    if sigs_total < 2:
        err = "You need more devices to do multisig"
        return render_template("base.jinja", specter=app.specter, rand=rand)
    sigs_required = sigs_total*2//3
    if sigs_required < 2:
        sigs_required = 2
    cosigners = []
    keys = []

    if request.method == 'POST':
        action = request.form['action']
        wallet_name = request.form['wallet_name']
        sigs_required = int(request.form['sigs_required'])
        sigs_total = int(request.form['sigs_total'])
        if wallet_name in app.specter.wallet_manager.wallets_names:
            err = "Wallet already exists"
        wallet_type = request.form['type']
        pur = {
            None: "General",
            "wsh": "Segwit (bech32)",
            "sh-wsh": "Nested Segwit",
            "sh": "Legacy",
        }
        if action == 'device' and err is None:
            cosigners = request.form.getlist('devices')
            if len(cosigners) != sigs_total:
                err = "Select all the cosigners"
            else:
                devices = []
                prefix = "tpub"
                if app.specter.chain == "main":
                    prefix = "xpub"
                for k in cosigners:
                    device = copy.deepcopy(app.specter.device_manager.devices[k])
                    allowed_types = ['', wallet_type]
                    device.keys = [key for key in device.keys if key.xpub.startswith(prefix) and key.key_type in allowed_types]
                    if len(device.keys) == 0:
                        err = "Device %s doesn't have keys matching this wallet type" % device.name
                    devices.append(device)
                return render_template("wallet/new_wallet/new_wallet_keys.jinja", purposes=pur, 
                    wallet_type=wallet_type, wallet_name=wallet_name, 
                    cosigners=devices, keys=keys, sigs_required=sigs_required, 
                    sigs_total=sigs_total, 
                    error=err, specter=app.specter, rand=rand)
        if action == 'key' and err is None:
            cosigners = []
            devices = []
            for i in range(sigs_total):
                try:
                    key = request.form['key%d' % i]
                    cosigner_name = request.form['cosigner%d' % i]
                    cosigner = app.specter.device_manager.devices[cosigner_name]
                    cosigners.append(cosigner)
                    for k in cosigner.keys:
                        if k.original == key:
                            keys.append(k)
                            break
                except:
                    pass
            if len(keys) != sigs_total or len(cosigners) != sigs_total:
                prefix = "tpub"
                if app.specter.chain == "main":
                    prefix = "xpub"
                for cosigner in cosigners:
                    device = copy.deepcopy(cosigner)
                    allowed_types = ['', wallet_type]
                    device.keys = [key for key in device.keys if key.xpub.startswith(prefix) and key.key_type in allowed_types]
                    devices.append(device)
                err = "Did you select all the keys?"
                return render_template("wallet/new_wallet/new_wallet_keys.jinja", purposes=pur, 
                    wallet_type=wallet_type, wallet_name=wallet_name, 
                    cosigners=devices, keys=keys, sigs_required=sigs_required, 
                    sigs_total=sigs_total, 
                    error=err, specter=app.specter, rand=rand)
            # create a wallet here
            wallet = app.specter.wallet_manager.create_multi(wallet_name, sigs_required, wallet_type, keys, cosigners)
            return redirect("/wallets/%s/" % wallet["alias"])
    return render_template("wallet/new_wallet/new_wallet.jinja", cosigners=cosigners, wallet_type=wallet_type, wallet_name=wallet_name, error=err, sigs_required=sigs_required, sigs_total=sigs_total, specter=app.specter, rand=rand)

@app.route('/wallets/<wallet_alias>/')
@login_required
def wallet(wallet_alias):
    app.specter.check()
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    if wallet.balance["untrusted_pending"] + wallet.balance["trusted"] == 0:
        return redirect("/wallets/%s/receive/" % wallet_alias)
    else:
        return redirect("/wallets/%s/tx/" % wallet_alias)

@app.route('/wallets/<wallet_alias>/tx/')
@login_required
def wallet_tx(wallet_alias):
    app.specter.check()
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet_tx: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    return render_template("wallet/history/txs/wallet_tx.jinja", wallet_alias=wallet_alias, wallet=wallet, specter=app.specter, rand=rand)

@app.route('/wallets/<wallet_alias>/addresses/', methods=['GET', 'POST'])
@login_required
def wallet_addresses(wallet_alias):
    app.specter.check()
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet_addresses: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    viewtype = 'address' if request.args.get('view') != 'label' else 'label'
    if request.method == "POST":
        action = request.form['action']
        if action == "updatelabel":
            label = request.form['label']
            account = request.form['account']
            if viewtype == 'address':
                wallet.setlabel(account, label)
            else:
                for address in wallet.addresses_on_label(account):
                    wallet.setlabel(address, label)
                wallet.getdata()
    alladdresses = True if request.args.get('all') != 'False' else False
    return render_template("wallet/history/addresses/wallet_addresses.jinja", wallet_alias=wallet_alias, wallet=wallet, alladdresses=alladdresses, viewtype=viewtype, specter=app.specter, rand=rand)

@app.route('/wallets/<wallet_alias>/receive/', methods=['GET', 'POST'])
@login_required
def wallet_receive(wallet_alias):
    app.specter.check()
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet_receive: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    if request.method == "POST":
        action = request.form['action']
        if action == "newaddress":
            wallet.getnewaddress()
        elif action == "updatelabel":
            label = request.form['label']
            wallet.setlabel(wallet['address'], label)
    if wallet.tx_on_current_address > 0:
        wallet.getnewaddress()
    return render_template("wallet/receive/wallet_receive.jinja", wallet_alias=wallet_alias, wallet=wallet, specter=app.specter, rand=rand)

@app.route('/get_fee/<blocks>')
@login_required
def fees(blocks):
    res = app.specter.estimatesmartfee(int(blocks))
    return res

@app.route('/wallets/<wallet_alias>/send/new', methods=['GET', 'POST'])
@login_required
def wallet_send(wallet_alias):
    app.specter.check()
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet_send: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    psbt = None
    address = ""
    label = ""
    amount = 0
    fee_rate = 0.0
    err = None
    if request.method == "POST":
        action = request.form['action']
        if action == "createpsbt":
            address = request.form['address']
            label = request.form['label']
            if request.form['label'] != "":
                wallet.setlabel(address, label)
            amount = float(request.form['btc_amount'])
            subtract = bool(request.form.get("subtract", False))
            fee_unit = request.form.get('fee_unit')
            selected_coins = request.form.getlist('coinselect')
            app.logger.info("selected coins: {}".format(selected_coins))
            if 'dynamic' in request.form.get('fee_options'):
                fee_rate = float(request.form.get('fee_rate_dynamic'))
            else:
                if request.form.get('fee_rate'):
                    fee_rate = float(request.form.get('fee_rate'))

            try:
                psbt = wallet.createpsbt(address, amount, subtract=subtract, fee_rate=fee_rate, fee_unit=fee_unit, selected_coins=selected_coins)
                if psbt is None:
                    err = "Probably you don't have enough funds, or something else..."
                else:
                    # calculate new amount if we need to subtract
                    if subtract:
                        for v in psbt["tx"]["vout"]:
                            if address in v["scriptPubKey"]["addresses"]:
                                amount = v["value"]
            except Exception as e:
                err = e
            if err is None:
                return render_template("wallet/send/sign/wallet_send_sign_psbt.jinja", psbt=psbt, label=label, 
                                                    wallet_alias=wallet_alias, wallet=wallet, 
                                                    specter=app.specter, rand=rand)
        elif action == "openpsbt":
            psbt = ast.literal_eval(request.form["pending_psbt"])
            return render_template("wallet/send/sign/wallet_send_sign_psbt.jinja", psbt=psbt, label=label, 
                                                wallet_alias=wallet_alias, wallet=wallet, 
                                                specter=app.specter, rand=rand)
        elif action == 'deletepsbt':
            try:
                wallet.delete_pending_psbt(ast.literal_eval(request.form["pending_psbt"])["tx"]["txid"])
            except Exception as e:
                flash("Could not delete Pending PSBT!")
    return render_template("wallet/send/new/wallet_send.jinja", psbt=psbt, label=label, 
                                                wallet_alias=wallet_alias, wallet=wallet, 
                                                specter=app.specter, rand=rand, error=err)

@app.route('/wallets/<wallet_alias>/send/pending/', methods=['GET', 'POST'])
@login_required
def wallet_sendpending(wallet_alias):
    app.specter.check()
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet_sendpending: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    if request.method == "POST":
        action = request.form['action']
        if action == 'deletepsbt':
            try:
                wallet.delete_pending_psbt(ast.literal_eval(request.form["pending_psbt"])["tx"]["txid"])
            except Exception as e:
                app.logger.error("Could not delete Pending PSBT: %s" % e)
                flash("Could not delete Pending PSBT!")
    pending_psbts = wallet.pending_psbts
    return render_template("wallet/send/pending/wallet_sendpending.jinja", pending_psbts=pending_psbts,
                                                wallet_alias=wallet_alias, wallet=wallet, 
                                                specter=app.specter) 


@app.route('/wallets/<wallet_alias>/settings/', methods=['GET','POST'])
@login_required
def wallet_settings(wallet_alias):
    app.specter.check()
    error = None
    try:
        wallet = app.specter.wallet_manager.get_by_alias(wallet_alias)
    except SpecterError as se:
        app.logger.error("SpecterError while wallet_receive: %s" % se)
        return render_template("base.jinja", error=se, specter=app.specter, rand=rand)
    if request.method == "POST":
        action = request.form['action']
        if action == "rescanblockchain":
            startblock = int(request.form['startblock'])
            try:
                res = wallet.cli.rescanblockchain(startblock, timeout=1)
            except requests.exceptions.ReadTimeout:
                # this is normal behaviour in our usecase
                pass
            except Exception as e:
                app.logger.error("%s while rescanblockchain" % e)
                error = "%r" % e
            wallet.getdata()
        elif action == "abortrescan":
            res = wallet.cli.abortrescan()
            if not res:
                error="Failed to abort rescan. Maybe already complete?"
            wallet.getdata()
        elif action == "keypoolrefill":
            delta = int(request.form['keypooladd'])
            wallet.keypoolrefill(wallet["keypool"], wallet["keypool"]+delta)
            wallet.keypoolrefill(wallet["change_keypool"], wallet["change_keypool"]+delta, change=True)
            wallet.getdata()
        elif action == "rebuildcache":
            wallet.cli.cache.rebuild_cache()
        elif action == "deletewallet":
            app.specter.wallet_manager.delete_wallet(wallet)
            response = redirect(url_for('index'))
            return response
        elif action == "rename":
            wallet_name = request.form['newtitle']
            if wallet_name in app.specter.wallet_manager.wallets_names:
                error = "Wallet already exists"
            else:
                app.specter.wallet_manager.rename_wallet(wallet, wallet_name)

    cc_file = None
    qr_text = wallet["name"]+"&"+wallet.descriptor
    if wallet.is_multisig:
        cc_file = wallet.get_cc_file()
        if cc_file is not None:
            cc_file = urllib.parse.quote(cc_file)
        return render_template("wallet/settings/wallet_settings.jinja", 
                            cc_file=cc_file, 
                            wallet_alias=wallet_alias, wallet=wallet, 
                            specter=app.specter, rand=rand, 
                            error=error,
                            qr_text=qr_text)
    else:
        return render_template("wallet/settings/wallet_settings.jinja", 
                            wallet_alias=wallet_alias, wallet=wallet, 
                            specter=app.specter, rand=rand, 
                            error=error,
                            qr_text=qr_text)

################# devices management #####################

@app.route('/new_device/', methods=['GET', 'POST'])
@login_required
def new_device():
    err = None
    app.specter.check()
    device_type = "other"
    device_name = ""
    xpubs = ""
    if request.method == 'POST':
        device_type = request.form['device_type']
        device_name = request.form['device_name']
        if not device_name:
            err = "Device name must not be empty"
        elif device_name in app.specter.device_manager.devices_names:
            err = "Device with this name already exists"
        xpubs = request.form['xpubs']
        if not xpubs:
            err = "xpubs name must not be empty"
        keys, failed = Key.parse_xpubs(xpubs)
        if len(failed) > 0:
            err = "Failed to parse these xpubs:\n" + "\n".join(failed)
        if err is None:
            device = app.specter.device_manager.add_device(name=device_name, device_type=device_type, keys=keys)
            return redirect("/devices/%s/" % device.alias)
    return render_template("device/new_device.jinja", device_type=device_type, device_name=device_name, xpubs=xpubs, error=err, specter=app.specter, rand=rand)

@app.route('/devices/<device_alias>/', methods=['GET', 'POST'])
@login_required
def device(device_alias):
    app.specter.check()
    try:
        device = app.specter.device_manager.get_by_alias(device_alias)
    except:
        return render_template("base.jinja", error="Device not found", specter=app.specter, rand=rand)
    if request.method == 'POST':
        action = request.form['action']
        if action == "forget":
            app.specter.device_manager.remove_device(device)
            return redirect("/")
        if action == "delete_key":
            key = request.form['key']
            device.remove_key(Key.from_json({ 'original': key }))
        if action == "add_keys":
            return render_template("device/new_device.jinja", device=device, specter=app.specter, rand=rand)
        if action == "morekeys":
            # refactor to fn
            xpubs = request.form['xpubs']
            keys, failed = Key.parse_xpubs(xpubs)
            err = None
            if len(failed) > 0:
                err = "Failed to parse these xpubs:\n" + "\n".join(failed)
                return render_template("device/new_device.jinja", device=device, xpubs=xpubs, error=err, specter=app.specter, rand=rand)
            if err is None:
                device.add_keys(keys)
    device = copy.deepcopy(device)
    device.keys.sort(key=lambda k: k.metadata["chain"] + k.metadata["purpose"], reverse=True)
    return render_template("device/device.jinja", device=device, purposes=purposes, specter=app.specter, rand=rand)



############### filters ##################

@app.template_filter('datetime')
def timedatetime(s):
    return format(datetime.fromtimestamp(s), "%d.%m.%Y %H:%M")

@app.template_filter('btcamount')
def btcamount(value):
    value = float(value)
    return "{:.8f}".format(value).rstrip("0").rstrip(".")
