import os
import pathlib
import uuid
import base64
from io import BytesIO
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate
from flask_socketio import SocketIO, emit, join_room, leave_room
import boto3
from sqlalchemy.exc import IntegrityError
from models import db, User, FriendRequest, Message
from stego import aes_encrypt, aes_decrypt, embed_text_into_png, extract_text_from_png

# ---------------- Environment Configuration ----------------
AWS_BUCKET = os.environ.get("AWS_S3_BUCKET")
AWS_REGION = os.environ.get("AWS_REGION") or "us-east-1"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///app.db")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")

# ---------------- Flask App Setup ----------------
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = 'login'  # redirect unauthenticated users to login
socketio = SocketIO(app, cors_allowed_origins="*")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# ---------------- AWS S3 Client ----------------
# boto3 client; operations may fail if credentials not set — code falls back to local storage
s3 = boto3.client(
    's3',
    region_name=AWS_REGION,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

# ---------------- Local Upload Fallback ----------------
UPLOADS_DIR = pathlib.Path(__file__).parent / "static" / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

def upload_to_s3(file_bytes: bytes, key: str, content_type='image/png'):
    """
    Upload to AWS S3 if configured; otherwise write to static/uploads.
    Returns a stored key string: either S3 key or "local:<filename>".
    """
    global s3, AWS_BUCKET
    if AWS_BUCKET:
        s3.put_object(
            Bucket=AWS_BUCKET,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
            ACL='private'
        )
        return key
    else:
        filename = pathlib.Path(key).name
        dest = UPLOADS_DIR / filename
        with open(dest, "wb") as f:
            f.write(file_bytes)
        return f"local:{filename}"

def get_s3_presigned(key: str, expires_in=3600):
    """
    Return a presigned S3 URL or a fully-qualified local static URL.
    """
    from flask import url_for
    if not key:
        return None
    if AWS_BUCKET and not key.startswith("local:"):
        return s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': AWS_BUCKET, 'Key': key},
            ExpiresIn=expires_in
        )
    if key.startswith("local:"):
        filename = key.split("local:", 1)[1]
        # return an absolute URL to the static uploads file
        return url_for('static', filename=f'uploads/{filename}', _external=True)
    return None

# ---------------- Flask-Login ----------------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------------- Routes ----------------
@app.route('/')
def home():
    return render_template('home.html')

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        confirm = request.form['confirm']
        mobile = request.form.get('mobile')
        address = request.form.get('address')

        if password != confirm:
            flash("Passwords do not match", "danger")
            return redirect(url_for('register'))

        # check uniqueness
        if User.query.filter_by(username=username).first():
            flash("Username already taken. Choose a different username.", "danger")
            return redirect(url_for('register'))
        if User.query.filter_by(email=email).first():
            flash("Email already registered", "danger")
            return redirect(url_for('register'))

        pw_hash = generate_password_hash(password)
        user = User(
            username=username,
            email=email,
            password_hash=pw_hash,
            mobile=mobile,
            address=address
        )

        # profile image
        file = request.files.get('profile_image')
        if file:
            key = f"profiles/{uuid.uuid4().hex}_{file.filename}"
            file_bytes = file.read()
            stored_key = upload_to_s3(file_bytes, key, content_type=file.content_type if hasattr(file, 'content_type') else 'image/png')
            user.profile_image = stored_key

        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Could not register user due to database constraint. Try different username/email.", "danger")
            return redirect(url_for('register'))

        flash("Registered successfully. Please login.", "success")
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('user_home'))
        flash("Invalid credentials", "danger")
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('home'))

@app.route('/user_home')
@login_required
def user_home():
    return render_template('user_home.html')

@app.route('/search', methods=['GET', 'POST'])
@login_required
def search():
    users = []
    if request.method == 'POST':
        q = request.form.get('q', '').strip()
        users = User.query.filter(
            User.username.ilike(f"%{q}%"),
            User.id != current_user.id
        ).all()
    return render_template('search.html', users=users)

@app.route('/send_request/<int:to_user_id>', methods=['POST'])
@login_required
def send_request(to_user_id):
    """Send a friend request (creates or re-sends if rejected)."""
    if to_user_id == current_user.id:
        flash("Cannot send a request to yourself", "danger")
        return redirect(url_for('search'))

    fr = FriendRequest.query.filter_by(from_user_id=current_user.id, to_user_id=to_user_id).first()
    if fr:
        # allow re-sending if previously rejected
        if hasattr(fr, 'status') and fr.status == 'rejected':
            fr.status = 'pending'
            db.session.commit()
            flash("Friend request re-sent", "success")
            return redirect(url_for('search'))
        flash(f"Request already {getattr(fr, 'status', 'sent')}", "info")
        return redirect(url_for('search'))

    new_req = FriendRequest(from_user_id=current_user.id, to_user_id=to_user_id, status='pending')
    db.session.add(new_req)
    try:
        db.session.commit()
        flash("Friend request sent", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Database error while sending request", "danger")
    return redirect(url_for('search'))

@app.route('/requests')
@login_required
def requests_page():
    # fetch pending incoming friend requests
    incoming = FriendRequest.query.filter_by(
        to_user_id=current_user.id,
        status='pending'
    ).order_by(FriendRequest.created_at.desc()).all()

    # build list of (request, from_user) tuples so template doesn't rely on relationships
    incoming_with_users = []
    for r in incoming:
        from_user = User.query.get(r.from_user_id)
        incoming_with_users.append((r, from_user))

    return render_template('requests.html', incoming=incoming_with_users)

@app.route('/handle_request/<int:req_id>/<action>', methods=['POST'])
@login_required
def handle_request(req_id, action):
    fr = FriendRequest.query.get_or_404(req_id)
    if fr.to_user_id != current_user.id:
        flash("Not allowed", "danger")
        return redirect(url_for('requests_page'))
    if action not in ['accept', 'reject']:
        flash("Invalid action", "danger")
        return redirect(url_for('requests_page'))
    fr.status = 'accepted' if action == 'accept' else 'rejected'
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Database error while updating request", "danger")
        return redirect(url_for('requests_page'))
    flash(f"Request {fr.status}", "success")
    return redirect(url_for('requests_page'))

@app.route('/select_friend')
@login_required
def select_friend():
    accepted = FriendRequest.query.filter(
        ((FriendRequest.from_user_id == current_user.id) |
         (FriendRequest.to_user_id == current_user.id)) &
        (FriendRequest.status == 'accepted')
    ).all()
    friends = []
    for fr in accepted:
        other_id = fr.to_user_id if fr.from_user_id == current_user.id else fr.from_user_id
        friends.append(User.query.get(other_id))
    return render_template('select_friend.html', friends=friends)

@app.route('/profile/<int:user_id>')
@login_required
def profile(user_id):
    user_obj = User.query.get_or_404(user_id)
    profile_url = None
    if user_obj.profile_image:
        profile_url = get_s3_presigned(user_obj.profile_image)
    return render_template('profile.html', user_obj=user_obj, profile_url=profile_url)

# ---------------- Steganography pages (UI) ----------------
@app.route('/encrypt')
@login_required
def encrypt_page():
    return render_template('encrypt.html')

@app.route('/decrypt')
@login_required
def decrypt_page():
    return render_template('decrypt.html')

# ---------------- Steganography & Chat API ----------------
@app.route('/<int:friend_id>')
@login_required
def chat(friend_id):
    friend = User.query.get_or_404(friend_id)
    return render_template('chat.html', friend=friend)

@app.route('/upload_stego', methods=['POST'])
@login_required
def upload_stego():
    """
    Accepts:
      - 'image' file (PNG/JPEG/any)
      - 'message' plaintext string
      - 'aes_key' (optional)
    Returns JSON:
      { "s3_key": "<key>", "download_url": "<url>" }
    The uploaded image is converted to PNG (RGBA) before embedding to ensure compatibility.
    """
    file = request.files.get('image')
    message = request.form.get('message', '')
    aes_key = request.form.get('aes_key', '')
    if not file:
        return jsonify({"error": "no file"}), 400

    # derive AES key (demo: sha256)
    from Crypto.Hash import SHA256
    key_hash = SHA256.new(aes_key.encode('utf-8') if aes_key else b'default-key')
    key_bytes = key_hash.digest()[:32]
    encrypted = aes_encrypt(key_bytes, message)

    # Force-open and convert to PNG (RGBA) to make embedding consistent
    try:
        from PIL import Image
        # Ensure we read the uploaded file from start
        file.stream.seek(0)
        img = Image.open(file.stream).convert("RGBA")
    except Exception as e:
        return jsonify({"error": "invalid image or could not convert to PNG", "detail": str(e)}), 400

    # embed encrypted bytes/text into PNG and get PNG bytes
    try:
        png_bytes = embed_text_into_png(img, encrypted)
    except Exception as e:
        return jsonify({"error": "embedding failed", "detail": str(e)}), 500

    # store as PNG
    s3_key = f"stegos/{uuid.uuid4().hex}.png"
    try:
        stored_key = upload_to_s3(png_bytes, s3_key, content_type='image/png')
    except Exception as e:
        # fallback: try to write local file directly if upload_to_s3 fails unexpectedly
        try:
            filename = pathlib.Path(s3_key).name
            dest = UPLOADS_DIR / filename
            with open(dest, "wb") as f:
                f.write(png_bytes)
            stored_key = f"local:{filename}"
        except Exception as ex:
            return jsonify({"error": "failed to store file", "detail": str(ex)}), 500

    # return a usable download URL (S3 presigned or local static URL)
    try:
        download_url = get_s3_presigned(stored_key)
    except Exception:
        download_url = None

    return jsonify({"s3_key": stored_key, "download_url": download_url})


@app.route('/decrypt_stego', methods=['POST'])
@login_required
def decrypt_stego():
    """
    Accepts:
      - 'image' file OR 's3_key' form field
      - 'aes_key' (optional)
    Returns JSON: { "message": "<plaintext>" } or { "error": ..., "detail": ... }
    """
    try:
        aes_key = request.form.get('aes_key', '')
        from Crypto.Hash import SHA256
        key_hash = SHA256.new(aes_key.encode('utf-8') if aes_key else b'default-key')
        key_bytes = key_hash.digest()[:32]

        # Debug log what was sent
        app.logger.debug("decrypt_stego: form keys=%s files=%s", list(request.form.keys()), list(request.files.keys()))
        app.logger.debug("decrypt_stego: s3_key=%s", request.form.get('s3_key'))

        file = request.files.get('image')
        s3_key = request.form.get('s3_key')
        png_bytes = None

        if file and getattr(file, 'filename', None):
            file.stream.seek(0)
            png_bytes = file.stream.read()
            app.logger.debug("Using uploaded file: %s (bytes=%d)", file.filename, len(png_bytes))
        elif s3_key:
            if s3_key.startswith("local:"):
                local_name = s3_key.split("local:", 1)[1]
                local_path = UPLOADS_DIR / local_name
                if not local_path.exists():
                    msg = f"local file not found: {local_path}"
                    app.logger.warning(msg)
                    return jsonify({"error":"local_file_not_found","detail":msg}), 400
                png_bytes = local_path.read_bytes()
                app.logger.debug("Loaded local file %s (bytes=%d)", local_path, len(png_bytes))
            else:
                if not AWS_BUCKET:
                    msg = "S3 key provided but AWS_BUCKET not configured"
                    app.logger.warning(msg)
                    return jsonify({"error":"s3_unavailable","detail":msg}), 400
                try:
                    obj = s3.get_object(Bucket=AWS_BUCKET, Key=s3_key)
                    png_bytes = obj['Body'].read()
                    app.logger.debug("Fetched from S3 %s (bytes=%d)", s3_key, len(png_bytes))
                except Exception as e:
                    app.logger.exception("Failed to fetch from S3")
                    return jsonify({"error":"s3_get_failed","detail":str(e)}), 400
        else:
            msg = "no input provided (upload a file or provide s3_key)"
            app.logger.warning(msg)
            return jsonify({"error":"no_input","detail":msg}), 400

        # extract and decrypt
        encrypted_text = extract_text_from_png(png_bytes)
        if not encrypted_text:
            msg = "no stego data found in provided image"
            app.logger.info(msg)
            return jsonify({"error":"no_stego","detail":msg}), 400

        try:
            plaintext = aes_decrypt(key_bytes, encrypted_text)
        except Exception as e:
            app.logger.exception("AES decryption failed")
            return jsonify({"error":"decrypt_failed","detail":str(e)}), 400

        return jsonify({"message": plaintext})
    except Exception as e:
        app.logger.exception("unexpected error in decrypt_stego")
        return jsonify({"error":"server_error","detail":str(e)}), 500



# ---------------- Socket.IO ----------------
@socketio.on('join')
def on_join(data):
    room = data.get('room')
    join_room(room)
    emit('status', {'msg': f"{data.get('username')} has entered the chat."}, room=room)

@socketio.on('leave')
def on_leave(data):
    room = data.get('room')
    leave_room(room)
    emit('status', {'msg': f"{data.get('username')} has left the chat."}, room=room)

@socketio.on('message')
def handle_message(data):
    """
    Handle a chat message: encrypt for storage, but send only plaintext to UI.
    """
    try:
        from Crypto.Hash import SHA256

        room = data.get('room')
        text = data.get('message', '').strip()
        aes_key = data.get('aes_key', 'default-key') or 'default-key'

        # Encrypt for database storage
        key_hash = SHA256.new(aes_key.encode('utf-8'))
        key_bytes = key_hash.digest()[:32]
        encrypted_text = aes_encrypt(key_bytes, text)

        # Store encrypted text in DB
        msg = Message(
            sender_id=data.get('sender_id'),
            receiver_id=data.get('receiver_id'),
            text_encrypted=encrypted_text
        )
        db.session.add(msg)
        db.session.commit()

        # Emit ONLY plaintext to the chat (no encrypted blob)
        emit('message', {
            'sender_id': data.get('sender_id'),
            'plaintext': text,
            'created_at': str(msg.created_at)
        }, room=room)

    except Exception as e:
        print("Socket message error:", e)
        emit('error', {'error': 'server_error', 'detail': str(e)})


# ---------------- Main Entry ----------------
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    print("App is running")
    port = int(os.environ.get('PORT', '5050'))
    socketio.run(app, host='0.0.0.0', port=port)
