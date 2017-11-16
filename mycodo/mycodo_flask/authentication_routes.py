# coding=utf-8
""" flask views that deal with user authentication """

import datetime
import logging
import socket
import time
import flask_login

from flask import redirect
from flask import request
from flask import render_template
from flask import flash
from flask import session
from flask import url_for
from flask import make_response

from sqlalchemy import func

from mycodo.mycodo_flask.extensions import db
from flask_babel import gettext
from flask.blueprints import Blueprint

from mycodo.databases.models import AlembicVersion
from mycodo.databases.models import DisplayOrder
from mycodo.databases.models import Misc
from mycodo.databases.models import Remote
from mycodo.databases.models import User

from mycodo.mycodo_flask.forms import forms_authentication
from mycodo.mycodo_flask.utils import utils_remote_host
from mycodo.mycodo_flask.utils import utils_general

from mycodo.utils.utils import test_username
from mycodo.utils.utils import test_password

from mycodo.config import LOGIN_ATTEMPTS
from mycodo.config import LOGIN_BAN_SECONDS
from mycodo.config import LOGIN_LOG_FILE


blueprint = Blueprint(
    'authentication_routes',
    __name__,
    static_folder='../static',
    template_folder='../templates'
)

logger = logging.getLogger(__name__)


@blueprint.context_processor
def inject_hostname():
    """Variables to send with every login page request"""
    return dict(host=socket.gethostname())


@blueprint.route('/create_admin', methods=('GET', 'POST'))
def create_admin():
    if admin_exists():
        flash(gettext(
            u"Cannot access admin creation form if an admin user "
            u"already exists."), "error")
        return redirect(url_for('general_routes.home'))

    # If login token cookie from previous session exists, delete
    if request.cookies.get('remember_token'):
        response = clear_cookie_auth()
        return response

    form_create_admin = forms_authentication.CreateAdmin()
    form_notice = forms_authentication.InstallNotice()

    if request.method == 'POST':
        form_name = request.form['form-name']
        if form_name == 'acknowledge':
            mod_misc = Misc.query.first()
            mod_misc.dismiss_notification = 1
            db.session.commit()
        elif form_create_admin.validate():
            username = form_create_admin.username.data.lower()
            error = False
            if form_create_admin.password.data != form_create_admin.password_repeat.data:
                flash(gettext(u"Passwords do not match. Please try again."),
                      "error")
                error = True
            if not test_username(username):
                flash(gettext(
                    u"Invalid user name. Must be between 2 and 64 characters "
                    u"and only contain letters and numbers."),
                    "error")
                error = True
            if not test_password(form_create_admin.password.data):
                flash(gettext(
                    u"Invalid password. Must be between 6 and 64 characters "
                    u"and only contain letters, numbers, and symbols."),
                      "error")
                error = True
            if error:
                return redirect(url_for('general_routes.home'))

            new_user = User()
            new_user.name = username
            new_user.email = form_create_admin.email.data
            new_user.set_password(form_create_admin.password.data)
            new_user.role = 1  # Admin
            new_user.theme = 'slate'
            try:
                db.session.add(new_user)
                db.session.commit()
                flash(gettext(u"User '%(user)s' successfully created. Please "
                              u"log in below.", user=username),
                      "success")
                return redirect(url_for('authentication_routes.do_login'))
            except Exception as except_msg:
                flash(gettext(u"Failed to create user '%(user)s': %(err)s",
                              user=username,
                              err=except_msg), "error")
        else:
            utils_general.flash_form_errors(form_create_admin)

    dismiss_notification = Misc.query.first().dismiss_notification

    return render_template('create_admin.html',
                           dismiss_notification=dismiss_notification,
                           form_create_admin=form_create_admin,
                           form_notice=form_notice)


@blueprint.route('/login', methods=('GET', 'POST'))
def do_login():
    """Authenticate users of the web-UI"""
    if not admin_exists():
        return redirect('/create_admin')

    elif flask_login.current_user.is_authenticated:
        flash(gettext(u"Cannot access login page if you're already logged in"),
              "error")
        return redirect(url_for('general_routes.home'))

    form_login = forms_authentication.Login()

    # Check if the user is banned from logging in (too many incorrect attempts)
    if banned_from_login():
        flash(gettext(
            u"Too many failed login attempts. Please wait %(min)s "
            u"minutes before attempting to log in again",
            min=(int(LOGIN_BAN_SECONDS - session['ban_time_left']) / 60) + 1),
                "info")
    else:
        if request.method == 'POST':
            username = form_login.username.data.lower()
            user_ip = request.environ.get('REMOTE_ADDR', 'unknown address')
            if form_login.validate_on_submit():
                user = User.query.filter(
                    func.lower(User.name) == username).first()
                if not user:
                    login_log(username, 'NA', user_ip, 'NOUSER')
                    failed_login()
                elif User().check_password(
                        form_login.password.data,
                        user.password_hash) == user.password_hash:

                    login_log(username, user.roles.name, user_ip, 'LOGIN')

                    # flask-login user
                    login_user = User()
                    login_user.id = user.id
                    remember_me = True if form_login.remember.data else False
                    flask_login.login_user(login_user, remember=remember_me)

                    return redirect(url_for('general_routes.home'))
                else:
                    login_log(username, user.roles.name, user_ip, 'FAIL')
                    failed_login()
            else:
                login_log(username, 'NA', user_ip, 'FAIL')
                failed_login()

            return redirect('/login')

    return render_template('login.html',
                           form_login=form_login,)


@blueprint.route("/logout")
@flask_login.login_required
def logout():
    """Log out of the web-ui"""
    login_log(flask_login.current_user.name,
              flask_login.current_user.roles.name,
              request.environ.get('REMOTE_ADDR', 'unknown address'),
              'LOGOUT')
    # flask-login logout
    flask_login.logout_user()

    response = clear_cookie_auth()

    flash(gettext(u"Successfully logged out"), 'success')
    return response


@blueprint.route('/remote/<page>', methods=('GET', 'POST'))
@flask_login.login_required
def remote_admin(page):
    """Return pages for remote administration"""
    if not utils_general.user_has_permission('edit_settings'):
        return redirect(url_for('general_routes.home'))

    remote_hosts = Remote.query.all()
    display_order_unsplit = DisplayOrder.query.first().remote_host
    if display_order_unsplit:
        display_order = display_order_unsplit.split(",")
    else:
        display_order = []

    if page == 'setup':
        form_setup = forms_authentication.RemoteSetup()
        host_auth = {}
        for each_host in remote_hosts:
            host_auth[each_host.host] = utils_remote_host.auth_credentials(
                each_host.host, each_host.username, each_host.password_hash)

        if request.method == 'POST':
            form_name = request.form['form-name']
            if form_name == 'setup':
                if form_setup.add.data:
                    utils_remote_host.remote_host_add(form_setup,
                                                      display_order)
            if form_name == 'mod_remote':
                if form_setup.delete.data:
                    utils_remote_host.remote_host_del(form_setup)
            return redirect('/remote/setup')

        return render_template('remote/setup.html',
                               form_setup=form_setup,
                               display_order=display_order,
                               remote_hosts=remote_hosts,
                               host_auth=host_auth)
    else:
        return render_template('404.html'), 404


@blueprint.route('/auth/')
def auth_remote():
    """Checks authentication for remote admin"""
    username = request.args.get('user')
    password_hash = request.args.get('pw_hash')

    user = User.query.filter(
        User.name == username).first()

    if (user and
            user.roles.name == 'Admin' and
            password_hash == user.password_hash):
        return "0"
    return "1"


def admin_exists():
    """Verify that at least one admin user exists"""
    return User.query.filter_by(role=1).count()


def check_database_version_issue():
    if len(AlembicVersion.query.all()) > 1:
        flash("A check of your database indicates there is an issue with your"
              " database version number. To resolve this issue, move"
              " your mycodo.db from ~/Mycodo/databases/mycodo.db to a "
              "different location (or delete it) and a new database will be "
              "generated in its place.", "error")


def banned_from_login():
    """Check if the person at the login prompt is banned form logging in"""
    if not session.get('failed_login_count'):
        session['failed_login_count'] = 0
    if not session.get('failed_login_ban_time'):
        session['failed_login_ban_time'] = 0
    elif session['failed_login_ban_time']:
        session['ban_time_left'] = time.time() - session['failed_login_ban_time']
        if session['ban_time_left'] < LOGIN_BAN_SECONDS:
            return 1
        else:
            session['failed_login_ban_time'] = 0
    return 0


def failed_login():
    """Count the number of failed login attempts"""
    try:
        session['failed_login_count'] += 1
    except KeyError:
        session['failed_login_count'] = 1

    if session['failed_login_count'] > LOGIN_ATTEMPTS - 1:
        session['failed_login_ban_time'] = time.time()
        session['failed_login_count'] = 0
    else:
        flash('Failed Login ({}/{})'.format(
            session['failed_login_count'], LOGIN_ATTEMPTS), "error")


def login_log(user, group, ip, status):
    """Write to login log"""
    with open(LOGIN_LOG_FILE, 'a') as log_file:
        log_file.write(
            '{dt:%Y-%m-%d %H:%M:%S}: {stat} {user} ({grp}), {ip}\n'.format(
                dt=datetime.datetime.now(), stat=status,
                user=user, grp=group, ip=ip))


def clear_cookie_auth():
    """Delete authentication cookies"""
    response = make_response(redirect('/login'))
    session.clear()
    response.set_cookie('remember_token', '', expires=0)
    return response
