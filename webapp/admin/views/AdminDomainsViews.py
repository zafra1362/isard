# Copyright 2017 the Isard-vdi project authors:
#      Josep Maria Viñolas Auquer
#      Alberto Larraz Dalmases
# License: AGPLv3

#!flask/bin/python
# coding=utf-8
import json
import time

from flask import render_template, Response
from flask_login import login_required, current_user

from webapp import app
from ...lib import admin_api

app.adminapi = admin_api.isardAdmin()

import rethinkdb as r
from ...lib.flask_rethink import RethinkDB
db = RethinkDB(app)
db.init_app(app)

from .decorators import isAdmin

'''
DOMAINS
'''
@app.route('/admin/domains')
@login_required
@isAdmin
def admin_domains():
    return render_template('admin/pages/domains.html', nav="Domains") 

@app.route('/admin/domains/get')
@login_required
@isAdmin
def admin_domains_get():
    return json.dumps(app.adminapi.get_admin_domains()), 200, {'ContentType': 'application/json'}

#~ @app.route('/admin/domains/datatables')
#~ @login_required
#~ @isAdmin
#~ def admin_domains_datatables():
    #~ return json.dumps(app.adminapi.get_admin_domain_datatables()), 200, {'ContentType': 'application/json'}

#~ @app.route('/admin/interfaces/get')
#~ @login_required
#~ @isAdmin
#~ def admin_interfaces_get():
    #~ return json.dumps(app.adminapi.get_admin_networks()), 200, {'ContentType': 'application/json'}
   
@app.route('/admin/stream/domains')
@login_required
@isAdmin
def admin_domains_stream():
        return Response(admin_domains_stream(), mimetype='text/event-stream')

def admin_domains_stream():
        #~ initial=True
        with app.app_context():
            for c in r.table('domains').changes(include_initial=False).run(db.conn):
                if c['new_val'] is None:
                    yield 'retry: 5000\nevent: %s\nid: %d\ndata: %s\n\n' % ('Deleted',time.time(),json.dumps(c['old_val']))
                    continue
                if c['old_val'] is None:
                    yield 'retry: 5000\nevent: %s\nid: %d\ndata: %s\n\n' % ('New',time.time(),json.dumps(app.isardapi.f.flatten_dict(c['new_val'])))   
                    continue             
                if 'detail' not in c['new_val']: c['new_val']['detail']=''
                yield 'retry: 2000\nevent: %s\nid: %d\ndata: %s\n\n' % ('Status',time.time(),json.dumps(app.isardapi.f.flatten_dict(c['new_val'])))