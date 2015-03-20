# coding: utf-8
from __future__ import absolute_import, unicode_literals, print_function

import os

import db
from sqlalchemy import or_, and_, asc, desc, func
from datetime import datetime
from functools import wraps # We need this to make Flask understand decorated routes.
import hashlib

from werkzeug import secure_filename
from flask import Flask, render_template, request, flash, redirect, session, abort, url_for, make_response, g
from wtforms import Form, BooleanField, TextField, TextAreaField, PasswordField, RadioField, SelectField, SelectMultipleField, BooleanField, HiddenField, SubmitField, validators, ValidationError, widgets

app_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask('rhforum', template_folder=app_dir+"/templates")
app.config.from_pyfile(app_dir+"/config.py") # XXX


class PostForm(Form):
    text = TextAreaField('Text', [validators.required()])
    submit = SubmitField('Odeslat')
    
class EditPostForm(Form):
    text = TextAreaField('Text', [validators.required()])
    submit = SubmitField('Upravit')
    
class EditThreadForm(Form):
    name = TextField('Nadpis', [validators.required()])
    text = TextAreaField('Text', [validators.required()])
    forum_id = SelectField('Fórum', coerce=int)
    submit = SubmitField('Upravit')

class ThreadForm(PostForm):
    name = TextField('Nadpis', [validators.required()])


@app.template_filter('datetime')
def datetime_format(value, format='%d. %m. %Y %H:%M:%S'):
    if not value: return "-"
    if isinstance(value, unicode): return value
    return value.strftime(format)

@app.before_request
def before_request():
    if 'user_id' in session:
        g.user = db.session.query(db.User).get(session['user_id'])
        if not g.user:
            # TODO
            pass
    else:
        g.user = None
    g.now = datetime.now()

@app.teardown_request
def shutdown_session(exception=None):
    db.session.close()
    db.session.remove()

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
    move_up = SubmitField('↑')
    move_down = SubmitField('↓')
    save = SubmitField('Uložit')
    delete = SubmitField('Odstranit')

@app.route("/")
def index():
    categories = db.session.query(db.Category).order_by(db.Category.position).all()
    uncategorized_fora = db.session.query(db.Forum).filter(db.Forum.category == None).order_by(db.Forum.position).all()
    if uncategorized_fora:
        categories.append(None)
    latest_posts = db.session.query(db.Post).filter(db.Post.deleted==False).order_by(db.Post.timestamp.desc())[0:10]
    return render_template("index.html", categories=categories, uncategorized_fora=uncategorized_fora, edit_forum = None, latest_posts=latest_posts)

@app.route("/edit-forum/<int:forum_id>", endpoint="edit_forum", methods="GET POST".split())
@app.route("/edit-forum/new", endpoint="edit_forum", methods="GET POST".split())
@app.route("/edit-catgory/<int:category_id>", endpoint="edit_category", methods="GET POST".split())
@app.route("/edit-category/new", endpoint="edit_category", methods="GET POST".split())
def edit_forum_or_category(forum_id=None, category_id=None):
    if not g.user.admin: abort(403) # TODO minrights decorator
    categories = db.session.query(db.Category).order_by(db.Category.position).all()
    uncategorized_fora = db.session.query(db.Forum).filter(db.Forum.category == None).order_by(db.Forum.position)
    if request.endpoint == 'edit_forum':
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
    elif request.endpoint == 'edit_category':
        if category_id:
            category = db.session.query(db.Category).get(category_id)
            #category.last = category.position == len(categories) - 1
        else:
            category = db.Category()
            categories = list(categories) + [category]
            category.position = 0
            category.last = True
        form = CategoryForm(request.form, category)
        editable = category
    if request.method == "POST" and form.validate():
        if request.endpoint == 'edit_forum':
            forum.name = form.name.data
            forum.identifier = forum.name.lower().replace(' ', '-')
            forum.description = form.description.data
            forum.category_id = form.category_id.data or None
            forum.category = db.session.query(db.Category).get(form.category_id.data)
        elif request.endpoint == 'edit_category':
            category.name = form.name.data
        if form.save.data:
            if request.endpoint == 'edit_forum':
                if not forum_id:
                    if forum.category_id:
                        forum.position = len(forum.category.fora) - 1
                    db.session.add(forum)
                    flash("Fórum vytvořeno.")
                else:
                    flash("Fórum upraveno.")
            elif request.endpoint == 'edit_category':
                if not category_id:
                    category.position = len(categories) - 1
                    db.session.add(category)
                    flash("Kategorie vytvořena.")
                else:
                    flash("Kategorie upravena.")
            db.session.commit()
            return redirect(url_for('index'))
        elif form.delete.data:
            if request.endpoint == 'edit_forum':
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
                    return redirect(url_for('index'))
            elif request.endpoint == 'edit_category':
                db.session.delete(category)
                flash("Kategorie odstraněna.")
                db.session.commit()
                return redirect(url_for('index'))
        else:
            # moving
            i = editable.position
            if request.endpoint == 'edit_forum':
                items = list(forum.category.fora)
            elif request.endpoint == 'edit_category':
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
            if request.endpoint == 'edit_category':
                categories = items
    if editable.position == 0:
        del form.move_up
    if request.endpoint == 'edit_forum':
        if not forum.category or forum.position == len(forum.category.fora) - 1:
            del form.move_down
    elif request.endpoint == 'edit_category':
        if not category.id or category.position == len(categories) - 1:
            del form.move_down
    return render_template("index.html", categories=categories+[None], uncategorized_fora=uncategorized_fora, editable=editable, form=form, new=not bool(forum_id))

class LoginForm(Form):
    name = TextField('Jméno', [validators.required()])
    password = PasswordField('Heslo', [validators.required()])
    submit = SubmitField('Přihlásit se')

@app.route("/login", methods="GET POST".split())
def login():
    form = LoginForm(request.form)
    failed = False
    if request.method == 'POST' and form.validate():
        user = db.session.query(db.User).filter(db.User.login == form.name.data.lower()).scalar()
        if not user: failed = True
        else:
            try:
                password_matches = user.verify_password(form.password.data)
            except db.OldHashingMethodException:
                failed = True
                password_matches = False
                flash("Prosím, změňte si heslo na DokuWiki..")
            if password_matches:
                g.user = user
                session['user_id'] = g.user.id
                session.permanent = True
                flash("Jste přihlášeni.")
                return redirect(url_for('index'))
            else:
                failed = True
    
    return render_template("login.html", form=form, failed=failed)

class RegisterForm(Form):
    name = TextField('Jméno', [validators.required()])
    email = TextField('Email', [validators.required()])
    submit = SubmitField('Zaregistrovat')

@app.route("/register", methods="GET POST".split())
def register():
    form = RegisterForm(request.form)
    '''
    if request.method == 'POST' and form.validate():
        user = db.User(name=form.name.data, email=form.email.data, timestamp=datetime.now())
        db.session.add(user)
        db.session.commit()
        g.user = user
        session['user_id'] = g.user.id
        session.permanent = True
        
        flash("Registrace proběhla úspěšně.")
        return redirect("/")
    '''
    
    return render_template("register.html", form=form)

@app.route("/logout")
def logout():
    if 'user_id' in session:
        session.pop('user_id')
        flash("Odhlášení proběhlo úspěšně.")
    return redirect(url_for('index'))

@app.route("/<int:forum_id>", methods="GET POST".split())
@app.route("/<int:forum_id>-<forum_identifier>", methods="GET POST".split())
def forum(forum_id, forum_identifier=None):
    if not g.user: abort(403)
    forum = db.session.query(db.Forum).get(forum_id)
    if not forum: abort(404)
    threads = db.session.query(db.Thread).filter(db.Thread.forum == forum).order_by(db.Thread.laststamp.desc())
    form = ThreadForm(request.form)
    if g.user and request.method == 'POST' and form.validate():
        now = datetime.now()
        thread = db.Thread(forum=forum, author=g.user, timestamp=now, laststamp=now,
            name=form.name.data)
        db.session.add(thread)
        post = db.Post(thread=thread, author=g.user, timestamp=now,
            text=form.text.data)
        db.session.add(post)
        db.session.commit()
        return redirect(thread.url)
    return render_template("forum.html", forum=forum, threads=threads, form=form)


# TODO <path:thread_identificator>
@app.route("/<int:forum_id>/<int:topic_id>", methods="GET POST".split())
@app.route("/<int:forum_id>-<forum_identifier>/<int:thread_id>-<thread_identifier>", methods="GET POST".split())
def thread(forum_id, thread_id, forum_identifier=None, thread_identifier=None):
    if not g.user: abort(403)
    thread = db.session.query(db.Thread).get(thread_id)
    if not thread: abort(404)
    posts = thread.posts.filter(db.Post.deleted==False)
    form = PostForm(request.form)
    if g.user and request.method == 'POST' and form.validate():
        now = datetime.now()
        post = db.Post(thread=thread, author=g.user, timestamp=now,
            text=form.text.data)
        db.session.add(post)
        thread.laststamp = now
        db.session.commit()
        return redirect(thread.url+"#latest") # TODO id
    
    return render_template("thread.html", thread=thread, forum=thread.forum, posts=posts, form=form, now=datetime.now())

@app.route("/<int:forum_id>/<int:thread_id>/edit/<int:post_id>", methods="GET POST".split())
@app.route("/<int:forum_id>-<forum_identifier>/<int:thread_id>-<thread_identifier>/edit/<int:post_id>", methods="GET POST".split())
def edit_post(forum_id, thread_id, post_id, forum_identifier=None, thread_identifier=None):
    if not g.user: abort(403)
    post = db.session.query(db.Post).get(post_id)
    thread = db.session.query(db.Thread).get(thread_id)
    if not post: abort(404)
    if post.thread != thread: abort(400)
    if post.author != g.user and not g.user.admin: abort(403)
    posts = thread.posts.filter(db.Post.deleted==False)
    
    if post == posts[0]:
        edit_thread = True
        form = EditThreadForm(request.form, text=post.text, name=thread.name, forum_id=thread.forum_id)
        forums = db.session.query(db.Forum).outerjoin(db.Category).order_by(db.Category.position, db.Forum.position).all()
        form.forum_id.choices = [(f.id, f.name) for f in forums]
    else:
        edit_thread = False
        form = EditPostForm(request.form, text=post.text)
    
    if request.method == 'POST' and form.validate():
        now = datetime.now()
        new_post = db.Post(thread=thread, author=g.user, timestamp=post.timestamp, editstamp=now,
            text=form.text.data, original=post.original if post.original else post)
        db.session.add(new_post)
        post.deleted=True
        if edit_thread:
           thread.name = form.name.data
           thread.forum_id = form.forum_id.data
           #forum.fix_laststamp() # TODO
        db.session.commit()
        if edit_thread:
            return redirect(thread.url)
        else:
            return redirect(new_post.url)
    
    return render_template("thread.html", thread=thread, forum=thread.forum, posts=posts, form=form, now=datetime.now(), edit_post=post, edit_thread=edit_thread)

@app.route("/users/<int:user_id>")
@app.route("/users/<int:user_id>-<name>")
def user(user_id, name=None):
    pass

if not app.debug:
    import logging
    from logging import FileHandler
    file_handler = FileHandler(app_dir+'/flask.log')
    file_handler.setLevel(logging.WARNING)
    app.logger.addHandler(file_handler)

if __name__ == "__main__":
    app.run(host="", port=8080, debug=True, threaded=True)











