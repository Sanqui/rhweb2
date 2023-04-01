#!/usr/bin/python
# coding: utf-8


from io import open

import os
import time
import re

import db
from sqlalchemy import or_, and_, not_, asc, desc, func
from datetime import datetime, timedelta
from functools import wraps # We need this to make Flask understand decorated routes.
import hashlib

import subprocess

from lxml.html.clean import Cleaner
from lxml.etree import ParserError

from werkzeug import secure_filename
from flask import Flask, Blueprint, render_template, request, flash, redirect, session, abort, url_for, make_response, g
from wtforms import Form, BooleanField, TextField, TextAreaField, PasswordField, RadioField, SelectField, SelectMultipleField, BooleanField, IntegerField, HiddenField, SubmitField, validators, ValidationError, widgets
from wtforms.fields.html5 import DateTimeLocalField

import requests

def now():
    if app.config['DB'].startswith('postgresql+psycopg2'):
        # https://stackoverflow.com/questions/796008/cant-subtract-offset-naive-and-offset-aware-datetimes/17752647#17752647
        import psycopg2
        return datetime.utcnow().replace(
            tzinfo=psycopg2.tz.FixedOffsetTimezone(offset=0, name=None))
    else:
        return datetime.utcnow()

dtnow = now

class MultiCheckboxField(SelectMultipleField):
    """
    A multiple-select, except displays a list of checkboxes.
    Iterating the field will produce subfields, allowing custom rendering of
    the enclosed checkbox fields.
    Shamelessly stolen from WTForms FAQ.
    """
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()

app_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask('rhforum', template_folder=app_dir+"/templates")
app.config.from_pyfile(app_dir+"/config.py") # XXX
BASE_URL = app.config.get("BASE_URL", "")

rhforum = Blueprint('rhforum', __name__,
    template_folder='templates',
    static_folder='static')

doku = None
if app.config.get("DOKU_URL", ""):
    from dokuwiki import DokuWiki
    try:
        doku = DokuWiki(app.config['DOKU_URL'], app.config['DOKU_USER'], app.config['DOKU_PASS'])
    except Exception as ex:
        print("Failed to connect to DokuWiki: ", ex)


class PostForm(Form):
    text = TextAreaField('Text', [validators.required()])
    submit = SubmitField('Odeslat')
    
class EditPostForm(Form):
    text = TextAreaField('Text', [validators.required()])
    submit = SubmitField('Upravit')
    delete = SubmitField('Smazat')
    
class EditThreadForm(Form):
    name = TextField('Nadpis', [validators.required()])
    text = TextAreaField('Text', [validators.required()])
    forum_id = SelectField('Fórum', coerce=int)
    wiki_article = TextField('Wiki článek')
    submit = SubmitField('Upravit')
    delete = SubmitField('Smazat')

class ThreadForm(PostForm):
    name = TextField('Nadpis', [validators.required()])

class UserForm(Form):
    fullname = TextField('Nadpis', [validators.required()])
    email = TextField('Email', [validators.required()])
    new_password = PasswordField('Nové heslo')
    homepage = TextField('Homepage')
    avatar_url = TextField('URL avataru')
    profile = TextAreaField('Profil')
    submit = SubmitField('Upravit')

class AdminUserForm(UserForm):
    group_ids = MultiCheckboxField('Skupiny', coerce=int)

@rhforum.app_template_filter('datetime')
def datetime_format(value, format='%d. %m. %Y %H:%M:%S'):
    if not value: return "-"
    if isinstance(value, str): return value
    return value.strftime(format)

cleaner = Cleaner(comments=False, style=False, embedded=False, annoying_tags=False)

@rhforum.app_template_filter('postfilter')
def postfilter(text):
    return text

@rhforum.app_template_filter('clean')
def clean(value):
    try:
        return cleaner.clean_html(value)
    except ParserError:
        return ""
    

@rhforum.app_template_filter('bbcode')
def bbcode(text):
    text = re.sub("\[quote=([^\]@]*)@(\d)*\]", "<blockquote><div class='quoting' data-id='\\2'>\\1</div><p>", text)
    text = re.sub("\[quote=([^\]@]*)\]", "<blockquote><div class='quoting'>\\1</div><p>", text)
    text = re.sub("\[quote\]", "<blockquote><p>", text)
    text = re.sub("\[\/quote\]", "</blockquote>", text)
    return text

@rhforum.before_request
def before_request():
    if not hasattr(g, 'telegram_messages'):
        g.telegram_messages = []
    if not hasattr(g, 'irc_messages'):
        g.irc_messages = []
    if not hasattr(g, 'discord_messages'):
        g.discord_messages = []
    if 'user_id' in session:
        g.user = db.session.query(db.User).get(session['user_id'])
        if not g.user:
            # TODO
            pass
        g.user.laststamp = now()
    else:
        g.user = db.Guest()
    g.now = now()
    g.yesterday = g.now - timedelta(days=1)
    g.tomorrow = g.now + timedelta(days=1)
    g.production = app.config['PRODUCTION']



@rhforum.after_request
def after_request(response):
    try:
        while g.telegram_messages:
            message = g.telegram_messages.pop(0)
            subprocess.Popen(["python", app_dir+"/report.py", "telegram", message.encode('utf-8')])
            
        while g.irc_messages:
            message = g.irc_messages.pop(0)
            subprocess.Popen(["python", app_dir+"/report.py", "irc", message.encode('utf-8')])
            
        while g.discord_messages:
            message = g.discord_messages.pop(0)
            subprocess.Popen(["python", app_dir+"/report.py", "discord", message.encode('utf-8')])
    except Exception as ex:
        print(type(ex), ex)
    
    return response
            

@rhforum.teardown_request
def shutdown_session(exception=None):
    db.session.close()
    db.session.remove()

def sort_tasks(tasks):
    return []
    now = g.now
    
    def cmp_tasks(task0, task1):
        # sort order:
        # 0. unspecified announcements and tasks
        # 1. upcoming announcements and all unfinished tasks
        # 2. past announcements and tasks ("everything else")
        # 3. finished unspecified tasks
        def get_task_priority(task):
            if not task.due_time and not task.status: return 0
            if not task.due_time and task.status == "todo": return 0
            if not task.status and task.due_time and task.due_time > now: return 1
            if task.status == "todo": return 1
            if not task.due_time and task.status == "done": return 3
            return 2
        task0_pri = get_task_priority(task0)
        task1_pri = get_task_priority(task1)
        if task0_pri < task1_pri: return -1
        if task0_pri > task1_pri: return 1
        if not task0.due_time: return 1;
        if not task1.due_time: return 1;
        return 1 if abs(now - task0.due_time) > abs(now - task1.due_time) else -1
    
    tasks.sort(cmp_tasks)

class ForumForm(Form):
    name = TextField('Jméno', [validators.required()])
    description = TextField('Popisek', [validators.required()])
    category_id = SelectField('Kategorie', coerce=int)
    move_up = SubmitField('↑')
    move_down = SubmitField('↓')
    save = SubmitField('Uložit')
    new_forum_id = SelectField('Nové fórum', coerce=int, default=0)
    delete = SubmitField('Odstranit')

class CategoryForm(Form):
    name = TextField('Jméno', [validators.required()])
    group_id = SelectField('Nutná skupina', coerce=int)
    move_up = SubmitField('↑')
    move_down = SubmitField('↓')
    save = SubmitField('Uložit')
    delete = SubmitField('Odstranit')

class ForumControlsForm(Form):
    mark_read = SubmitField('Označit fórum za přečtené')

class TaskForm(Form):
    type = SelectField("Typ", [validators.optional()], choices=(('task', 'úkol'), ('announcement', 'oznámení')))
    due_time = DateTimeLocalField('Čas', [validators.optional()], format="%Y-%m-%dT%H:%M")
    text = TextField('Text', [validators.required()])
    user_id = SelectField('Uživatel', coerce=int)
    submit = SubmitField("Zadat")
    
@rhforum.errorhandler(404)
def page_not_found(e):
    if not request.path.startswith("/static"):
        return render_template('forum/errorpage.html', error=404), 404
    else:
        return "404", 404 # we don't have templates

@rhforum.errorhandler(403)
def page_not_found(e):
    return render_template('forum/errorpage.html', error=403), 403

@rhforum.errorhandler(500)
def page_not_found(e):
    return render_template('forum/errorpage.html', error=500), 500
    
@rhforum.errorhandler(400)
def page_not_found(e):
    return render_template('forum/errorpage.html', error=400), 400

def get_active_threads():
    threads = db.session.query(db.Thread).join(db.Forum).outerjoin(db.Category)\
        .filter(or_(db.Forum.category_id==None, db.Category.group_id.in_([None, 0]), db.Category.group_id.in_(group.id for group in g.user.groups)))\
        .filter(db.Forum.trash == False) \
        .order_by(db.Thread.laststamp.desc())
    
    return threads

@rhforum.route("/", methods="GET POST".split())
def index():
    form = None
    if g.user:
        form = ForumControlsForm(request.form)
        if request.method == "POST":# and form.validate():
            if form.mark_read.data:
                g.user.read_all()
    
    categories = db.session.query(db.Category).order_by(db.Category.position).all()
    uncategorized_fora = db.session.query(db.Forum).filter(db.Forum.category == None, db.Forum.trash == False).order_by(db.Forum.position).all()
    trash = db.session.query(db.Forum).filter(db.Forum.trash == True).scalar()
    if uncategorized_fora:
        categories.append(None)
    latest_threads = get_active_threads()[0:10]
    
    tasks = db.session.query(db.Task).filter(db.Task.user_id.in_([g.user.id, None, 0])).all()
    sort_tasks(tasks)
    
    return render_template("forum/index.html", categories=categories, uncategorized_fora=uncategorized_fora, edit_forum = None, latest_threads=latest_threads, trash=trash, form=form, tasks=tasks)

@rhforum.route("/active", methods="GET POST".split())
def active():
    form = ForumControlsForm(request.form)
    active_threads = get_active_threads()[0:100]
    return render_template("forum/active.html", active_threads=active_threads, form=form)

@rhforum.route("/edit-forum/<int:forum_id>", endpoint="edit_forum", methods="GET POST".split())
@rhforum.route("/edit-forum/new", endpoint="edit_forum", methods="GET POST".split())
@rhforum.route("/edit-category/<int:category_id>", endpoint="edit_category", methods="GET POST".split())
@rhforum.route("/edit-category/new", endpoint="edit_category", methods="GET POST".split())
def edit_forum_or_category(forum_id=None, category_id=None):
    if not g.user.admin: abort(403) # TODO minrights decorator
    categories = db.session.query(db.Category).order_by(db.Category.position).all()
    uncategorized_fora = db.session.query(db.Forum).filter(db.Forum.category == None, db.Forum.trash == False).order_by(db.Forum.position)
    trash = db.session.query(db.Forum).filter(db.Forum.trash == True).scalar()
    if request.endpoint == 'rhforum.edit_forum':
        if forum_id:
            forum = db.session.query(db.Forum).get(forum_id)
            #forum.last = forum.position == len(forum.category.fora) - 1 if forum.category else True
            if not forum.category: forum.position = 0
        else:
            forum = db.Forum()
            uncategorized_fora = list(uncategorized_fora) + [forum]
            forum.position = 0
            forum.last = True
        form = ForumForm(request.form, forum)
        form.category_id.choices = [(0, "-")] + [(c.id, c.name) for c in categories if c]
        fora = db.session.query(db.Forum).outerjoin(db.Category).order_by(db.Category.position, db.Forum.position).all()
        form.new_forum_id.choices = [(0, "-")] + [(f.id, f.name) for f in fora]
        editable = forum
    elif request.endpoint == 'rhforum.edit_category':
        if category_id:
            category = db.session.query(db.Category).get(category_id)
            #category.last = category.position == len(categories) - 1
        else:
            category = db.Category()
            categories = list(categories) + [category]
            category.position = 0
            category.last = True
        form = CategoryForm(request.form, category)
        form.group_id.choices = [(0, "-")] + [(group.id, group.name) for group in db.session.query(db.Group)]
        editable = category
    if request.method == "POST" and form.validate():
        if request.endpoint == 'rhforum.edit_forum':
            forum.name = form.name.data
            forum.identifier = forum.name.lower().replace(' ', '-')
            forum.description = form.description.data
            forum.category_id = form.category_id.data or None
            forum.category = db.session.query(db.Category).get(form.category_id.data)
        elif request.endpoint == 'rhforum.edit_category':
            category.name = form.name.data
            category.group_id = form.group_id.data
        if form.save.data:
            if request.endpoint == 'rhforum.edit_forum':
                if not forum_id:
                    if forum.category_id:
                        forum.position = len(forum.category.fora) - 1
                    db.session.add(forum)
                    flash("Fórum vytvořeno.")
                else:
                    flash("Fórum upraveno.")
            elif request.endpoint == 'rhforum.edit_category':
                if not category_id:
                    category.position = len(categories) - 1
                    db.session.add(category)
                    flash("Kategorie vytvořena.")
                else:
                    flash("Kategorie upravena.")
            db.session.commit()
            return redirect(url_for('.index'))
        elif form.delete.data:
            if request.endpoint == 'rhforum.edit_forum':
                if not form.new_forum_id.data and forum.threads:
                    flash("Je nutno témata někam přesunout.")
                else:
                    moved = False
                    if form.new_forum_id.data:
                        moved = True
                        new_forum = db.session.query(db.Forum).get(form.new_forum_id.data)
                        for thread in forum.threads:
                            thread.forum = new_forum
                        else:
                            moved = False
                    db.session.delete(forum)
                    if moved:
                        flash("Fórum odstraněno a témata přesunuty.")
                    else:
                        flash("Fórum odstraněno.")
                    db.session.commit()
                    return redirect(url_for('.index'))
            elif request.endpoint == 'rhforum.edit_category':
                db.session.delete(category)
                flash("Kategorie odstraněna.")
                db.session.commit()
                return redirect(url_for('.index'))
        else:
            # moving
            i = editable.position
            if request.endpoint == 'rhforum.edit_forum':
                items = list(forum.category.fora)
            elif request.endpoint == 'rhforum.edit_category':
                items = list(categories)
            items.remove(editable)
            if form.move_up and form.move_up.data:
                items.insert(i-1, editable)
            elif form.move_down and form.move_down.data:
                items.insert(i+1, editable)
            for i, x in enumerate(items):
                x.position = i
                db.session.add(x)
            db.session.commit()
            if request.endpoint == 'rhforum.edit_category':
                categories = items
    if editable.position == 0:
        del form.move_up
    if request.endpoint == 'rhforum.edit_forum':
        if not forum.category or forum.position == len(forum.category.fora) - 1:
            del form.move_down
    elif request.endpoint == 'rhforum.edit_category':
        if not category.id or category.position == len(categories) - 1:
            del form.move_down
    return render_template("forum/index.html", categories=categories+[None], uncategorized_fora=uncategorized_fora, editable=editable, form=form, new=not bool(forum_id), trash=trash)

class LoginForm(Form):
    name = TextField('Jméno', [validators.required()])
    password = PasswordField('Heslo', [validators.required()])
    submit = SubmitField('Přihlásit se')

@rhforum.route("/login", methods="GET POST".split())
def login():
    form = LoginForm(request.form)
    failed = False
    if request.method == 'POST' and form.validate():
        user = db.session.query(db.User).filter(db.User.login == form.name.data.lower()).first()
        if not user: failed = True
        else:
            try:
                password_matches = user.verify_password(form.password.data)
            except db.OldHashingMethodException:
                failed = True
                password_matches = False
                flash("Prosím, požádejte admina o změnu hesla.")
            if password_matches:
                g.user = user
                session['user_id'] = g.user.id
                session.permanent = True
                flash("Jste přihlášeni.")
                return redirect(url_for('.index'))
            else:
                failed = True
    
    return render_template("forum/login.html", form=form, failed=failed)

class RegisterForm(Form):
    username = TextField('Nevyplňovat')
    bbq = TextField('Login', [validators.required()])
    fullname = TextField('Jméno', [validators.required()])
    password = PasswordField('Heslo', [
        validators.Required(),
        validators.EqualTo('confirm_password', message='Hesla se musí schodovat')
    ])
    confirm_password = PasswordField('Heslo znovu')
    email = TextField('Email', [validators.required()])
    submit = SubmitField('Zaregistrovat se')

@rhforum.route("/register", methods="GET POST".split())
def register():
    if g.user:
        if g.user.admin:
            flash("Pro ruční registraci účtů ostatním použijte prosím DokuWiki.")
        return redirect(url_for(".index"))
    form = RegisterForm(request.form)
    if request.method == 'POST' and form.validate():
        if form.username.data:
            return "OK"
        username = form.bbq.data.lower()
        if db.session.query(db.User).filter(db.User.login == username).first():
            flash("Tento login je už zabraný, vyberte si prosím jiný.")
        else:
            user = db.User(login=username, fullname=form.fullname.data, email=form.email.data, timestamp=now(), laststamp=now())
            user.set_password(form.password.data)
            user_group = db.session.query(db.Group).filter(db.Group.name=="user").scalar()
            if user_group:
                user.groups.append(user_group)
            db.session.add(user)
            db.session.commit()
            
            g.telegram_messages.append("Nová registrace: *{}* (login *{}*, email {}): {}".format(
                user.fullname, user.login, user.email, BASE_URL+user.url))
            #g.irc_messages.append("Nová registrace: \x0302{}\x03 (login \x0208{}\x03, email {}): {}".format(
            #    user.fullname, user.login, user.email, BASE_URL+user.url))
            g.discord_messages.append("Nová registrace: **{}** (login **{}**, email {}): <{}>".format(
                user.fullname, user.login, user.email, BASE_URL+user.url))
            
            g.user = user
            g.user.read_all()
            session['user_id'] = g.user.id
            session.permanent = True
            
            flash("Registrace proběhla úspěšně.")
            return redirect(url_for(".index"))
    
    return render_template("forum/register.html", form=form)

@rhforum.route("/logout")
def logout():
    if 'user_id' in session:
        session.pop('user_id')
        flash("Odhlášení proběhlo úspěšně.")
    return redirect(url_for('.index'))

@rhforum.route("/<int:forum_id>", methods="GET POST".split())
@rhforum.route("/<int:forum_id>-<forum_identifier>", methods="GET POST".split())
def forum(forum_id, forum_identifier=None):
    forum = db.session.query(db.Forum).get(forum_id)
    if not forum: abort(404)
    if forum.category and forum.category.group and forum.category.group not in g.user.groups: abort(403)
    if forum.trash and not g.user.admin: abort(403)
    threads = db.session.query(db.Thread).filter(db.Thread.forum == forum).order_by(db.Thread.archived.asc(), db.Thread.pinned.desc(), db.Thread.laststamp.desc())
    form = None
    if not forum.trash:
        form = ThreadForm(request.form)
        if g.user and request.method == 'POST' and form.validate():
            now = dtnow()
            thread = db.Thread(forum=forum, author=g.user, timestamp=now, laststamp=now,
                name=form.name.data)
            db.session.add(thread)
            post = db.Post(thread=thread, author=g.user, timestamp=now,
                text=form.text.data)
            db.session.add(post)
            db.session.commit()
            g.telegram_messages.append("Nové téma od *{}*: *{}*: {}".format(
                thread.author.name, thread.name, BASE_URL+thread.short_url))
            if not thread.forum.category or not thread.forum.category.group or thread.forum.category.group.name == "extern":
                g.discord_messages.append("Nové téma od **{}**: **{}**: <{}>".format(
                    thread.author.name, thread.name, BASE_URL+thread.short_url))
            #    g.irc_messages.append("Nové téma od \x0302{}\x03: \x0306{}\x03: {}".format(
            #        thread.author.name, thread.name, BASE_URL+thread.short_url))
            return redirect(thread.url)
    return render_template("forum/forum.html", forum=forum, threads=threads, form=form)

@rhforum.route("/users/<int:user_id>/threads")
@rhforum.route("/users/<int:user_id>-<name>/threads")
def user_threads(user_id, name=None):
    user = db.session.query(db.User).get(user_id)
    if not user: abort(404)
    
    forum = db.Forum(name="Témata od {}".format(user.name))
    
    threads = db.session.query(db.Thread).join(db.Forum)\
        .filter(db.Forum.trash == False, db.Thread.author == user)\
        .outerjoin(db.Category)\
        .filter(or_(db.Forum.category_id==None, db.Category.group_id.in_([None, 0]), db.Category.group_id.in_(group.id for group in g.user.groups)))\
        .filter(db.Forum.trash == False).order_by(db.Thread.laststamp.desc()).all()
    
    return render_template("forum/forum.html", forum=forum, threads=threads, user=user)
    

# TODO <path:thread_identificator>
@rhforum.route("/<int:forum_id>/<int:thread_id>", methods="GET POST".split())
@rhforum.route("/<int:forum_id>-<forum_identifier>/<int:thread_id>-<thread_identifier>", methods="GET POST".split())
def thread(forum_id, thread_id, forum_identifier=None, thread_identifier=None):
    thread = db.session.query(db.Thread).get(thread_id)
    if not thread: abort(404)
    if thread.forum.category and thread.forum.category.group and thread.forum.category.group not in g.user.groups: abort(403)
    if thread.forum.trash and not g.user.admin: abort(403)
    reply_post = None
    if "reply" in request.args:
        try:
            reply_post_id = int(request.args["reply"])
        except ValueError:
            abort(400)
        reply_post = db.session.query(db.Post).get(reply_post_id)
        if reply_post_id and not reply_post:
            abort(404)
        if reply_post and reply_post.thread != thread:
            abort(400)
    
    if g.user.admin and "show_deleted" in request.args:
        posts = thread.posts.filter()
    else:
        posts = thread.posts.filter(db.Post.deleted==False)
    
    num_deleted = thread.posts.count() - thread.posts.filter(db.Post.deleted==False).count()
    
    form = None
    if not thread.forum.trash and not (thread.locked and not g.user.admin):
        text = ""
        if reply_post:
            text = "[quote={}@{}]{}[/quote]\n".format(reply_post.author.login, reply_post.id, reply_post.text)
        form = PostForm(request.form, text=text)
        if g.user and request.method == 'POST' and form.validate():
            now = dtnow()
            post = db.Post(thread=thread, author=g.user, timestamp=now,
                text=form.text.data)
            db.session.add(post)
            thread.laststamp = now
            db.session.commit()
            g.telegram_messages.append("Nový příspěvek od *{}* do *{}*: {}".format(
                post.author.name, post.thread.name, BASE_URL+post.short_url))
            if not thread.forum.category or not thread.forum.category.group or thread.forum.category.group.name == "extern":
                g.discord_messages.append("Nový příspěvek od **{}** do **{}**: <{}>".format(
                    post.author.name, post.thread.name, BASE_URL+post.short_url))
            #    g.irc_messages.append("Nový příspěvek od \x0302{}\x03 do \x0306{}\x03: {}".format(
            #        post.author.name, post.thread.name, BASE_URL+post.short_url))
            return redirect(thread.url+"#post-latest") # TODO id
    
    if g.user:
        thread_read = db.session.query(db.ThreadRead).filter(db.ThreadRead.user==g.user, db.ThreadRead.thread==thread).first()
        if not thread_read:
            last_read_timestamp = None
        else:
            last_read_timestamp = thread_read.last_post.timestamp
        g.user.read(thread.last_post)
    else:
        last_read_timestamp = g.now
        
    article = None
    article_revisions = []
    article_info = None
    doku_error = None
    if thread.wiki_article and doku:
        try:
            article = doku.pages.html(thread.wiki_article)
            #article_revisions = doku.send("wiki.getPageVersions", thread.wiki_article)
            article_info = doku.send("wiki.getPageInfo", thread.wiki_article)
            print(article_info, 'xxx')
        except Exception as ex:
            print(ex)
            doku_error = ex
    
    return render_template("forum/thread.html", thread=thread, forum=thread.forum, posts=posts, form=form, now=dtnow(), last_read_timestamp=last_read_timestamp, article=article, article_revisions=article_revisions, article_info=article_info, doku_error=doku_error, reply_post=reply_post, show_deleted="show_deleted" in request.args, num_deleted=num_deleted)

@rhforum.route("/<int:forum_id>/<int:topic_id>/set", methods="POST".split())
@rhforum.route("/<int:forum_id>-<forum_identifier>/<int:thread_id>-<thread_identifier>/set", methods="POST".split())
def thread_set(forum_id, thread_id, forum_identifier=None, thread_identifier=None):
    if not g.user.admin: abort(403)
    thread = db.session.query(db.Thread).get(thread_id)
    if not thread: abort(404)
    
    if request.form.get("pin"):
        thread.pinned = True
    elif request.form.get("unpin"):
        thread.pinned = False
        
    elif request.form.get("lock"):
        thread.locked = True
    elif request.form.get("unlock"):
        thread.locked = False
        
    elif request.form.get("archive"):
        thread.archived = True
    elif request.form.get("unarchive"):
        thread.archived = False
    db.session.commit()
    
    return redirect(thread.url)

@rhforum.route("/<int:forum_id>/<int:thread_id>/edit/<int:post_id>", methods="GET POST".split())
@rhforum.route("/<int:forum_id>-<forum_identifier>/<int:thread_id>-<thread_identifier>/edit/<int:post_id>", methods="GET POST".split())
def edit_post(forum_id, thread_id, post_id, forum_identifier=None, thread_identifier=None):
    post = db.session.query(db.Post).get(post_id)
    thread = db.session.query(db.Thread).get(thread_id)
    if not post: abort(404)
    if thread.forum.category and thread.forum.category.group and thread.forum.category.group not in g.user.groups: abort(403)
    if post.thread != thread: abort(400)
    if post.deleted:
        # The user probably hit edit multiple times.  Let's just be helpful.
        return redirect(thread.url)
    if post.author != g.user and not g.user.admin: abort(403)
    if post.thread.forum.trash and not g.user.admin: abort(403)
    posts = thread.posts.filter(db.Post.deleted==False)
    
    if post == posts[0] and g.user.admin:
        edit_thread = True
        form = EditThreadForm(request.form, text=post.text, name=thread.name, forum_id=thread.forum_id, wiki_article=thread.wiki_article)
        forums = db.session.query(db.Forum).outerjoin(db.Category).order_by(db.Category.position, db.Forum.position).all()
        form.forum_id.choices = [(f.id, f.name) for f in forums]
    else:
        edit_thread = False
        form = EditPostForm(request.form, text=post.text)
    
    if not g.user.admin: del form.delete
    
    if request.method == 'POST' and form.validate():
        if form.submit.data:
            now = dtnow()
            new_post = db.Post(thread=thread, author=post.author, timestamp=post.timestamp, editstamp=now,
                text=form.text.data, original=post.original if post.original else post, editor=g.user)
            db.session.add(new_post)
            post.deleted=True
            if edit_thread:
               thread.name = form.name.data
               thread.forum_id = form.forum_id.data
               thread.wiki_article = form.wiki_article.data
               #forum.fix_laststamp() # TODO
            db.session.commit()
            if edit_thread:
                return redirect(thread.url)
            else:
                return redirect(new_post.url)
        elif form.delete.data:
            post.deleted = True
            db.session.commit()
            return redirect(thread.url)
    
    return render_template("forum/thread.html", thread=thread, forum=thread.forum, posts=posts, form=form, now=dtnow(), edit_post=post, edit_thread=edit_thread, last_read_timestamp=g.now)

@rhforum.route("/users/")
def users():
    if not g.user.admin: abort(403)
    users = db.session.query(db.User).order_by(db.User.fullname)
    return render_template("forum/users.html", users=users)

@rhforum.route("/users/<int:user_id>")
@rhforum.route("/users/<int:user_id>-<name>")
def user(user_id, name=None):
    user = db.session.query(db.User).get(user_id)
    if not user: abort(404)
    return render_template("forum/user.html", user=user)

@rhforum.route("/users/<int:user_id>/edit", methods="GET POST".split())
@rhforum.route("/users/<int:user_id>-<name>/edit", methods="GET POST".split())
def edit_user(user_id, name=None):
    user = db.session.query(db.User).get(user_id)
    if not user: abort(404)
    if user != g.user and not g.user.admin: abort(403)
    
    if g.user.admin:
        form = AdminUserForm(request.form, user)
        form.group_ids.choices = []
        for group in db.session.query(db.Group):
            form.group_ids.choices.append((group.id, group.name))
        if form.group_ids.data == None:
            form.group_ids.data = [group.id for group in user.groups]
    else:
        form = UserForm(request.form, user)
        
        
    if request.method == 'POST' and form.validate():
        user.fullname = form.fullname.data
        user.email = form.email.data
        user.homepage = form.homepage.data
        user.avatar_url = form.avatar_url.data
        if form.new_password.data:
            user.set_password(form.new_password.data)
            flash("Heslo změněno.")
        if g.user.admin:
            user.groups = []
            for group_id in form.group_ids.data:
                user.groups.append(db.session.query(db.Group).get(group_id))
        db.session.commit()
        flash("Uživatel upraven.")
        return redirect(user.url)
    
    return render_template("forum/user.html", user=user, edit=True, form=form)

class GroupForm(Form):
    name = TextField('Jméno', [validators.required()])
    symbol = TextField('Symbol')
    title = TextField('Titul')
    rank = IntegerField('Rank')
    display = BooleanField('Zobrazovat')
    submit = SubmitField('Uložit')
    

@rhforum.route("/groups/", methods=["GET"])
@rhforum.route("/groups/<int:edit_group_id>/edit", methods=["GET", "POST"])
def groups(edit_group_id=None):
    if not g.user.admin: abort(403)
    groups = db.session.query(db.Group).all()
    edit_group = None
    form = None
    if edit_group_id == 0 and request.method == 'POST':
        group = db.Group(name="")
        db.session.add(group)
        db.session.commit()
        return redirect(url_for('.groups', edit_group_id=group.id))
    if edit_group_id:
        edit_group = db.session.query(db.Group).get(edit_group_id)
        form = GroupForm(request.form, edit_group)
    if request.method == 'POST' and form.validate():
        edit_group.name = form.name.data
        edit_group.symbol = form.symbol.data
        edit_group.title = form.title.data
        edit_group.rank = form.rank.data
        edit_group.display = form.display.data
        db.session.commit()
        flash("Skupina {} upravena.".format(edit_group.name))
        return redirect(url_for('.groups'))
    
    return render_template("forum/groups.html", groups=groups, edit_group=edit_group, form=form)

@rhforum.route("/tasks", methods="GET POST".split())
@rhforum.route("/tasks/<int:task_id>", methods=["GET", "POST"])
def tasks(task_id=None):
    if not g.user.in_group("retroherna"): error(403)
    task = None
    if task_id:
        task = db.session.query(db.Task).get(task_id)
        if not task: error(404)
    
    form = TaskForm(request.form, task)
    form.user_id.choices = [(0, '-')]
    for user in db.session.query(db.User):
        form.user_id.choices.append((user.id, user.name))
    
    if request.method == 'POST' and form.validate():
        if not form.due_time.data and (form.type.data == "announcement" or (task and not task.status)):
            flash("Nelze vytvořit oznámení bez konečného času.")
        else:
            if not task_id:
                task = db.Task()
                task.created_time = now()
                task.author = g.user
            task.text = form.text.data
            task.due_time = form.due_time.data
            if form.type.data == "task":
                task.status = "todo"
            task.user_id = form.user_id.data
            
            if not task_id:
                db.session.add(task)
            db.session.commit()
            if not task_id:
                flash("Úkol přidán.")
            else:
                flash("Úkol upraven.")
            return redirect(url_for('.tasks'))
    
    tasks = db.session.query(db.Task).all()#.order_by(func.abs(func.now() - db.Task.due_time))
    sort_tasks(tasks)
    
    return render_template("forum/tasks.html", tasks=tasks, form=form, task_id=task_id)

@rhforum.route("/tasks/<int:task_id>/status", methods=["POST"])
def change_task_status(task_id):
    if not g.user.in_group("retroherna"): error(403)
    task = db.session.query(db.Task).get(task_id)
    if not task: error(404)
    if request.form["status"] == "todo":
        task.status = "todo"
    elif request.form["status"] == "done":
        task.status = "done"
    db.session.commit()
    return redirect(url_for(".tasks"))


class IRCSendForm(Form):
    text = TextField('Text', [validators.required()])
    submit = SubmitField('Odeslat')

@rhforum.route("/irc-send/", methods=["GET", "POST"])
def irc_send():
    if not g.user.admin: error(403)
    
    text = None
    form = IRCSendForm(request.form)
    if request.method == 'POST' and form.validate():
        text = form.text.data
        g.irc_messages.append(text)
        
        form = IRCSendForm()
    
    return render_template("forum/irc_send.html", form=form, text=text)

app.register_blueprint(rhforum, url_prefix='')

if not app.debug:
    import logging
    from logging import FileHandler
    file_handler = FileHandler(app_dir+'/flask.log')
    file_handler.setLevel(logging.WARNING)
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    file_handler.setFormatter(formatter)
    app.logger.addHandler(file_handler)

if __name__ == "__main__":
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.run(host="", port=8080, debug=True, threaded=True)











