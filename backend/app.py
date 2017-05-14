from __future__ import print_function
import base64
import sys, os, time
import glob, json, argparse, copy
import tempfile
import webbrowser
from wsgiref.simple_server import WSGIRequestHandler, make_server
import bottle
from bottle import Bottle, static_file, request, debug, template
from serial_manager import SerialManagerClass
from flash import flash_upload, reset_atmega
from build import build_firmware
from filereaders import read_svg, read_dxf, read_ngc
from serial import SerialException
import serial
import i18n
import datedecoder
import readid
import os.path
from odoo_remote import OdooRemote
import SensorShield
import uuid
from odooHelper import *

bottle.BaseRequest.MEMFILE_MAX = 20 * 1024 * 1024  # 20MB max upload

APPNAME = "lasaurapp"
VERSION = "14.11b"
CONFIG_FILE = "config.json"

configfile = os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILE)
print("loading config file", configfile)
with open(configfile) as configdata:
    config = json.load(configdata)

COMPANY_NAME = config.get("company_name", "com.nortd.labs")
SERIAL_PORT = config.get("serial_port", None)
BITSPERSECOND = config.get("bitspersecond", 57600)
NETWORK_PORT = config.get("network_port", 4444)
HARDWARE = config.get("hardware", 'beaglebone')  # also: 'x86', 'raspberrypi'
COOKIE_KEY = config.get("cookie_key", os.urandom(10))
FIRMWARE = config.get("firmware", "LasaurGrbl.hex")
TOLERANCE = config.get("tolerance", 0.08)
I18N = i18n.Translations(config.get("language", "de"))
ACCOUNTING_FILE = config.get("accounting", {}).get("outputfile", "logs/accounting.json")
INFLUX_CONFIG = config.get("influx", False)
USE_ID_CARD_ACCESS_RESTRICTION = config.get("use_id_card_access_restriction", False)
ODOO_USERNAME = config.get("odoo_username", "admin")
ODOO_PASSWORD = config.get("odoo_password", "admin")
ODOO_URL = config.get("odoo_url", "http://127.0.0.1:8069")
ODOO_DB = config.get("odoo_db", "testDB")
ODOO_USE = config.get("odoo_use", False)
IDCARD_TIMEOUT = config.get("idcard_timeout", 10)
SENSOR_SHIELD_PORT = config.get("sensor_shield_port", None)
SENSOR_SHIELD_BAUD = config.get("sensor_shield_baud", None)

SerialManager = SerialManagerClass(ACCOUNTING_FILE, INFLUX_CONFIG, False)
odooremote = OdooRemote(ODOO_USERNAME, ODOO_PASSWORD, ODOO_URL, ODOO_DB, ODOO_USE)
sensor_serial = None
dummy_mode = False
session_info = []

lastCardCheck = 0

if os.name == 'nt':  # sys.platform == 'win32':
    GUESS_PREFIX = "Arduino"
elif os.name == 'posix':
    if sys.platform == "linux" or sys.platform == "linux2":
        GUESS_PREFIX = "2341"  # match by arduino VID
    else:
        GUESS_PREFIX = "tty.usbmodem"
else:
    GUESS_PREFIX = "no prefix"


def pauseIfCardNotAvailable():
    global lastCardCheck
    if USE_ID_CARD_ACCESS_RESTRICTION:
        if (time.time() - lastCardCheck) > IDCARD_TIMEOUT:
            lastCardCheck = time.time()
            if not has_valid_id():
                SerialManager.set_pause(True)


def setDummyMode():
    odooremote.dummy_mode = True
    global SerialManager
    SerialManager = SerialManagerClass(ACCOUNTING_FILE, INFLUX_CONFIG, True)
    dummy_mode = True


def resources_dir():
    """This is to be used with all relative file access.
       _MEIPASS is a special location for data files when creating
       standalone, single file python apps with pyInstaller.
       Standalone is created by calling from 'other' directory:
       python pyinstaller/pyinstaller.py --onefile app.spec
    """
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    else:
        # root is one up from this file
        return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '../'))


def storage_dir():
    directory = ""
    if sys.platform == 'darwin':
        # from AppKit import NSSearchPathForDirectoriesInDomains
        # # NSApplicationSupportDirectory = 14
        # # NSUserDomainMask = 1
        # # True for expanding the tilde into a fully qualified path
        # appdata = path.join(NSSearchPathForDirectoriesInDomains(14, 1, True)[0], APPNAME)
        directory = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', COMPANY_NAME, APPNAME)
    elif sys.platform == 'win32':
        directory = os.path.join(os.path.expandvars('%APPDATA%'), COMPANY_NAME, APPNAME)
    else:
        directory = os.path.join(os.path.expanduser('~'), "." + APPNAME)

    if not os.path.exists(directory):
        os.makedirs(directory)

    return directory


class HackedWSGIRequestHandler(WSGIRequestHandler):
    """ This is a heck to solve super slow request handling
    on the BeagleBone and RaspberryPi. The problem is WSGIRequestHandler
    which does a reverse lookup on every request calling gethostbyaddr.
    For some reason this is super slow when connected to the LAN.
    (adding the IP and name of the requester in the /etc/hosts file
    solves the problem but obviously is not practical)
    """

    def address_string(self):
        """Instead of calling getfqdn -> gethostbyaddr we ignore."""
        # return "(a requester)"
        return str(self.client_address[0])

    def log_request(*args, **kw):
        # if debug:
        # return wsgiref.simple_server.WSGIRequestHandler.log_request(*args, **kw)
        pass


sensor_names = None
sensor_values = None


def run_with_callback(host, port):
    """ Start a wsgiref server instance with control over the main loop.
        This is a function that I derived from the bottle.py run()
    """
    server = make_server(host, port, app, handler_class=HackedWSGIRequestHandler)
    server.timeout = 0.01
    server.quiet = True
    print("Persistent storage root is: " + storage_dir())
    print("-----------------------------------------------------------------------------")
    print("Bottle server starting up ...")
    print("Serial is set to %d bps" % BITSPERSECOND)
    print("Point your browser to: ")
    print("http://%s:%d/      (local)" % ('127.0.0.1', port))
    # if host == '':
    # try:
    #         print "http://%s:%d/   (public)" % (socket.gethostbyname(socket.gethostname()), port)
    #     except socket.gaierror:
    #         # print "http://beaglebone.local:4444/      (public)"
    #         pass
    print("Use Ctrl-C to quit.")
    print("-----------------------------------------------------------------------------")
    print("")
    # auto-connect on startup
    global SERIAL_PORT
    if not SERIAL_PORT:
        SERIAL_PORT = SerialManager.match_device(GUESS_PREFIX, BITSPERSECOND)
    SerialManager.connect(SERIAL_PORT, BITSPERSECOND)

    global sensor_names
    global sensor_values
    sensor_serial = None
    if SENSOR_SHIELD_PORT and SENSOR_SHIELD_BAUD and not dummy_mode:
        print("Initializing Sensor Board!")
        try:
            sensor_serial = serial.Serial(SENSOR_SHIELD_PORT, SENSOR_SHIELD_BAUD, timeout=5)
            # print("Sensor Shield at " + sensor_serial.name + + " with baudrate " + SENSOR_SHIELD_BAUD + " is (hopefully) ready!")
            print(sensor_serial)
            time.sleep(1)
            sensor_serial.flushInput()
            print(sensor_serial.readline())
            str = sensor_serial.readline().replace('\r\n', '')
            print(str)
            str = str.split(';')
            sensor_names = [0.00] * len(str)
            sensor_values = [0.00] * len(str)
            for i in range(0, len(str), 1):
                sensor_names[i] = str[i]
            print(sensor_names)
        except(SerialException):
            sensor_serial = None
            print("COULD NOT CONNECT TO SENSOR BOARD!")

    # open web-browser
    if config.get("open_browser", True):
        try:
            webbrowser.open_new_tab('http://127.0.0.1:' + str(port))
            pass
        except:
            print("Cannot open Webbrowser, please do so manually at http://127.0.0.1:" + str(port))

    sys.stdout.flush()  # make sure everything gets flushed
    server.timeout = 0
    while 1:
        try:
            SerialManager.send_queue_as_ready()
            server.handle_request()
            if sensor_serial and sensor_serial.inWaiting() > 10:
                str = sensor_serial.readline().split(';')
                for i in range(0, len(str), 1):
                    sensor_values[i] = float(str[i])
                    # print(sensor_values)
            pauseIfCardNotAvailable()

            time.sleep(0.0004)
        except KeyboardInterrupt:
            break
    print("\nShutting down...")
    SerialManager.close()


def clean_id(id):
    if not isinstance(id, basestring):
        return None
    # remove comment
    id = id.split('#', 1)[0]

    # remove separators
    id = id.replace('-', '')
    id = id.replace(':', '')
    id = id.replace(' ', '')

    # remove any left over whitespace
    id = id.strip()

    return id.lower()


def has_valid_id():
    if not USE_ID_CARD_ACCESS_RESTRICTION:
        return True
    id = clean_id(readid.getId())
    return odooremote.check_access(id)


# @app.route('/longtest')
# def longtest_handler():
# fp = open("longtest.ngc")
#     for line in fp:
#         SerialManager.queue_gcode_line(line)
#     return "Longtest queued."

app = Bottle()


@app.route('/css/:path#.+#')
def static_css_handler(path):
    return static_file(path, root=os.path.join(resources_dir(), 'frontend/css'))


@app.route('/js/:path#.+#')
def static_js_handler(path):
    return static_file(path, root=os.path.join(resources_dir(), 'frontend/js'))


@app.route('/img/:path#.+#')
def static_img_handler(path):
    return static_file(path, root=os.path.join(resources_dir(), 'frontend/img'))


@app.route('/fonts/:path#.+#')
def static_img_handler(path):
    return static_file(path, root=os.path.join(resources_dir(), 'frontend/fonts'))


@app.route('/favicon.ico')
def favicon_handler():
    return static_file('favicon.ico', root=os.path.join(resources_dir(), 'frontend/img'))


### LIBRARY

@app.route('/library/get/:path#.+#')
def static_library_handler(path):
    return static_file(path, root=os.path.join(resources_dir(), 'library'), mimetype='text/plain')


@app.route('/library/list')
def library_list_handler():
    # return a json list of file names
    file_list = []
    cwd_temp = os.getcwd()
    try:
        os.chdir(os.path.join(resources_dir(), 'library'))
        file_list = glob.glob('*')
    finally:
        os.chdir(cwd_temp)
    return json.dumps(file_list)


### ID

@app.route('/has_valid_id')
def has_valid_id_handler():
    return json.dumps(has_valid_id())


### QUEUE

def encode_filename(name):
    str(time.time()) + '-' + base64.urlsafe_b64encode(name)


def decode_filename(name):
    index = name.find('-')
    return base64.urlsafe_b64decode(name[index + 1:])


@app.route('/accounting')
def accounting():
    return json.dumps(SerialManager.job_accounting)


@app.route('/jobs/history')
def jobs_history():
    jobs = SerialManager.lastJobs
    if "limit" in request.params.keys():
        limit = int(request.params["limit"])
        jobs = jobs[:limit]
    return json.dumps(jobs, default=datedecoder.default)


@app.route('/material/services')
def material_services():
    return json.dumps(odooremote.services, default=datedecoder.default)


@app.route('/material/products')
def material_products():
    return json.dumps(odooremote.materials, default=datedecoder.default)


@app.route('/sensors/names')
def get_sensorNames():
    # out = list()
    # for i in range(0, len(sensor_names), 1):
    #    out += [(zip({"Name", "Value", "Symbol"}, {sensor_names[i], sensor_values[i], ""}))]
    # print(dict(zip(["Sensor"] * len(sensor_names), out)))
    # print(out)
    return json.dumps((sensor_names))


@app.route('/sensors/values')
def get_sensor_values():
    return json.dumps(sensor_values)


@app.route('/material/set_service/<id>')
def material_set_service(id):
    print("Setting Odoo Service ID: " + str(id))
    SerialManager.odoo_service = odooremote.get_service(id)
    return None


@app.route('/material/set_product/<id>')
def material_set_service(id):
    print("Setting Odoo Material ID: " + str(id))
    SerialManager.odoo_product = odooremote.get_product(id)


@app.route('/material/set_comment/<comment>')
def material_set_comment(comment):
    print("Comment: " + str(comment))
    SerialManager.job_comment = str(comment)


@app.route('/material/get_sell_mode')
def get_sell_mode():
    return str(ODOO_USE)


@app.route('/material/getCutSpeed')
def get_cut_speed():
    print(SerialManager.odoo_product['machine_parameter_1'])
    return SerialManager.odoo_product['machine_parameter_1']


@app.route('/material/getCutIntensity')
def get_cut_intensity():
    return SerialManager.odoo_product['machine_parameter_2']


@app.route('/material/getEngraveSpeed')
def get_engrave_speed():
    return SerialManager.odoo_product['machine_parameter_3']


@app.route('/material/getEngraveIntensity')
def get_engrave_intensity():
    return SerialManager.odoo_product['machine_parameter_4']


@app.route('/checkLogin', method='POST')
def checkLogin():
    session_id = bottle.request.get_cookie('session_id')
    user_name = bottle.request.get_cookie('user_name')
    if not session_id or not user_name:
        return "false"
    info = next(item for item in session_info if item['session_id'] == session_id)
    if not info:
        return "false"
    if info['user_name'] != user_name:
        return "false"
    return "true"


@app.route('/login', method='POST')
def login():
    login_email = request.forms.get('login_email')
    login_password = request.forms.get('login_password')  # TODO dont save plaintext passwords
    if not login_email:
        return "Email missing"
    if not login_password:
        return "Password missing"
    helper = OdooHelper(login_email, login_password, ODOO_URL, ODOO_DB)
    uid = helper.callAPI('/machine_management/getCurrentUser')
    if not uid:
        return "Couldn't find Odoo User"
    print(uid)
    info = {
        'session_id': str(uuid.uuid4()),
        'odoo_uid': uid,
        'user_name': login_email
    }
    session_info.append(info)
    print(session_info)
    bottle.response.set_cookie('user_name', info['user_name'])
    bottle.response.set_cookie('session_id', info['session_id'])
    return "true"


@app.route('/queue/get/:name#.+#')
def static_queue_handler(name):
    return static_file(name, root=storage_dir(), mimetype='text/plain')


@app.route('/queue/list')
def library_list_handler():
    # base64.urlsafe_b64encode()
    # base64.urlsafe_b64decode()
    # return a json list of file names
    files = []
    cwd_temp = os.getcwd()
    try:
        os.chdir(storage_dir())
        files = filter(os.path.isfile, glob.glob("*"))
        files.sort(key=lambda x: os.path.getmtime(x))
    finally:
        os.chdir(cwd_temp)
    return json.dumps(files)


@app.route('/queue/save', method='POST')
def queue_save_handler():
    ret = '0'
    if 'job_name' in request.forms and 'job_data' in request.forms:
        name = request.forms.get('job_name')
        job_data = request.forms.get('job_data')
        filename = os.path.abspath(os.path.join(storage_dir(), name.strip('/\\')))
        if os.path.exists(filename) or os.path.exists(filename + '.starred'):
            return "file_exists"

        fp = None
        try:
            fp = open(filename, 'w')
            fp.write(job_data)
            print("file saved: " + filename)
            ret = '1'
        finally:
            if fp:
                fp.close()
    else:
        print("error: save failed, invalid POST request")
    return ret


@app.route('/queue/rm/:name')
def queue_rm_handler(name):
    # delete queue item, on success return '1'
    ret = '0'
    filename = os.path.abspath(os.path.join(storage_dir(), name.strip('/\\')))
    if filename.startswith(storage_dir()):
        if os.path.exists(filename):
            try:
                os.remove(filename)
                print("file deleted: " + filename)
                ret = '1'
            finally:
                pass
    return ret


@app.route('/queue/clear')
def queue_clear_handler():
    # delete all queue items, on success return '1'
    ret = '0'
    files = []
    cwd_temp = os.getcwd()
    try:
        os.chdir(storage_dir())
        files = filter(os.path.isfile, glob.glob("*"))
        files.sort(key=lambda x: os.path.getmtime(x))
    finally:
        os.chdir(cwd_temp)
    for filename in files:
        if not filename.endswith('.starred'):
            filename = os.path.join(storage_dir(), filename)
            try:
                os.remove(filename)
                print("file deleted: " + filename)
                ret = '1'
            finally:
                pass
    return ret


@app.route('/queue/star/:name')
def queue_star_handler(name):
    ret = '0'
    filename = os.path.abspath(os.path.join(storage_dir(), name.strip('/\\')))
    if filename.startswith(storage_dir()):
        if os.path.exists(filename):
            os.rename(filename, filename + '.starred')
            ret = '1'
    return ret


@app.route('/queue/unstar/:name')
def queue_unstar_handler(name):
    ret = '0'
    filename = os.path.abspath(os.path.join(storage_dir(), name.strip('/\\')))
    if filename.startswith(storage_dir()):
        if os.path.exists(filename + '.starred'):
            os.rename(filename + '.starred', filename)
            ret = '1'
    return ret


@app.route('/')
@app.route('/index.html')
@app.route('/app.html')
def default_handler():
    filename = os.path.join(os.path.join(resources_dir(), 'frontend', 'app.html'))
    return template(filename, list(I18N))


@app.route('/stash_download', method='POST')
def stash_download():
    """Create a download file event from string."""
    filedata = request.forms.get('filedata')
    fp = tempfile.NamedTemporaryFile(mode='w', delete=False)
    filename = fp.name
    with fp:
        fp.write(filedata)
        fp.close()
    print(filedata)
    print("file stashed: " + os.path.basename(filename))
    return os.path.basename(filename)


@app.route('/download/:filename/:dlname')
def download(filename, dlname):
    print("requesting: " + filename)
    return static_file(filename, root=tempfile.gettempdir(), download=dlname)


@app.route('/serial/:connect')
def serial_handler(connect):
    if connect == '1':
        # print 'js is asking to connect serial'
        if not SerialManager.is_connected():
            try:
                global SERIAL_PORT, BITSPERSECOND, GUESS_PREFIX
                if not SERIAL_PORT:
                    SERIAL_PORT = SerialManager.match_device(GUESS_PREFIX, BITSPERSECOND)
                SerialManager.connect(SERIAL_PORT, BITSPERSECOND)
                ret = "Serial connected to %s:%d." % (SERIAL_PORT, BITSPERSECOND) + '<br>'
                time.sleep(1.0)  # allow some time to receive a prompt/welcome
                SerialManager.flush_input()
                SerialManager.flush_output()
                return ret
            except SerialException:
                SERIAL_PORT = None
                print("Failed to connect to serial.")
                return ""
    elif connect == '0':
        # print 'js is asking to close serial'
        if SerialManager.is_connected():
            if SerialManager.close():
                return "1"
            else:
                return ""
    elif connect == "2":
        # print 'js is asking if serial connected'
        if SerialManager.is_connected():
            return "1"
        else:
            return ""
    else:
        print('ambigious connect request from js: ' + connect)
        return ""


@app.route('/status')
def get_status():
    status = copy.deepcopy(SerialManager.get_hardware_status())
    status['serial_connected'] = SerialManager.is_connected()
    status['lasaurapp_version'] = VERSION
    status['has_valid_id'] = has_valid_id()
    return json.dumps(status)


@app.route('/pause/:flag')
def set_pause(flag):
    if not has_valid_id():
        return '0'

    # returns pause status
    if flag == '1':
        if SerialManager.set_pause(True):
            print("pausing ...")
            return '1'
        else:
            return '0'
    elif flag == '0':
        print("resuming ...")
        if SerialManager.set_pause(False):
            return '1'
        else:
            return '0'


@app.route('/flash_firmware')
@app.route('/flash_firmware/:firmware_file')
def flash_firmware_handler(firmware_file=FIRMWARE):
    global SERIAL_PORT, GUESS_PREFIX

    return_code = 1
    if SerialManager.is_connected():
        SerialManager.close()
    # get serial port by url argument
    # e.g: /flash_firmware?port=COM3
    if 'port' in request.GET.keys():
        serial_port = request.GET['port']
        if serial_port[:3] == "COM" or serial_port[:4] == "tty.":
            SERIAL_PORT = serial_port
    # get serial port by enumeration method
    # currenty this works on windows only for updating the firmware
    if not SERIAL_PORT:
        SERIAL_PORT = SerialManager.match_device(GUESS_PREFIX, BITSPERSECOND)
    # resort to brute force methode
    # find available com ports and try them all
    if not SERIAL_PORT:
        comport_list = SerialManager.list_devices(BITSPERSECOND)
        for port in comport_list:
            print("Trying com port: " + port)
            return_code = flash_upload(port, resources_dir(), firmware_file, HARDWARE)
            if return_code == 0:
                print("Success with com port: " + port)
                SERIAL_PORT = port
                break
    else:
        return_code = flash_upload(SERIAL_PORT, resources_dir(), firmware_file, HARDWARE)
    ret = []
    ret.append('Using com port: %s<br>' % (SERIAL_PORT))
    ret.append('Using firmware: %s<br>' % (firmware_file))
    if return_code == 0:
        print("SUCCESS: Arduino appears to be flashed.")
        ret.append('<h2>Successfully Flashed!</h2><br>')
        ret.append('<a href="/">return</a>')
        return ''.join(ret)
    else:
        print("ERROR: Failed to flash Arduino.")
        ret.append('<h2>Flashing Failed!</h2> Check terminal window for possible errors. ')
        ret.append('Most likely LasaurApp could not find the right serial port.')
        ret.append(
            '<br><a href="/flash_firmware/' + firmware_file + '">try again</a> or <a href="/">return</a><br><br>')
        if os.name != 'posix':
            ret.append('If you know the COM ports the Arduino is connected to you can specifically select it here:')
            for i in range(1, 13):
                ret.append('<br><a href="/flash_firmware?port=COM%s">COM%s</a>' % (i, i))
        return ''.join(ret)


@app.route('/build_firmware')
def build_firmware_handler():
    ret = []
    buildname = "LasaurGrbl_from_src"
    firmware_dir = os.path.join(resources_dir(), 'firmware')
    source_dir = os.path.join(resources_dir(), 'firmware', 'src')
    return_code = build_firmware(source_dir, firmware_dir, buildname)
    if return_code != 0:
        print(ret)
        ret.append('<h2>FAIL: build error!</h2>')
        ret.append('Syntax error maybe? Try builing in the terminal.')
        ret.append('<br><a href="/">return</a><br><br>')
    else:
        print("SUCCESS: firmware built.")
        ret.append('<h2>SUCCESS: new firmware built!</h2>')
        ret.append('<br><a href="/flash_firmware/' + buildname + '.hex">Flash Now!</a><br><br>')
    return ''.join(ret)


@app.route('/reset_atmega')
def reset_atmega_handler():
    if not has_valid_id():
        print("ERROR: Failed to reset Chip. No Valid ID entered.")
        return '0'

    reset_atmega(HARDWARE)
    return '1'


@app.route('/gcode', method='POST')
def job_submit_handler():
    if not has_valid_id():
        print("cancel gcode post request because no valid ID is inserted")
        return "no_id"

    name = request.forms.get('name')
    job_data = request.forms.get('job_data')
    if job_data and SerialManager.is_connected():
        SerialManager.queue_gcode(job_data, name, odooremote.last_user)
        return "__ok__"
    else:
        return "serial disconnected"


@app.route('/queue_pct_done')
def queue_pct_done_handler():
    return SerialManager.get_queue_percentage_done()


@app.route('/file_reader', method='POST')
def file_reader():
    """Parse SVG string."""
    filename = request.forms.get('filename')
    filedata = request.forms.get('filedata')
    dimensions = request.forms.get('dimensions')
    try:
        dimensions = json.loads(dimensions)
    except TypeError:
        dimensions = None
    # print "dims", dimensions[0], ":", dimensions[1]


    dpi_forced = None
    try:
        dpi_forced = float(request.forms.get('dpi'))
    except:
        pass

    optimize = True
    try:
        optimize = bool(int(request.forms.get('optimize')))
    except:
        pass

    try:
        if filename and filedata:
            print("You uploaded %s (%d bytes)." % (filename, len(filedata)))
            if filename[-4:] in ['.dxf', '.DXF']:
                res = read_dxf(filedata, TOLERANCE, optimize)
            elif filename[-4:] in ['.svg', '.SVG']:
                res = read_svg(filedata, dimensions, TOLERANCE, dpi_forced, optimize)
            elif filename[-4:] in ['.ngc', '.NGC']:
                res = read_ngc(filedata, TOLERANCE, optimize)
            else:
                print("error: unsupported file format")
                return None

            # print boundarys
            jsondata = json.dumps(res)
            # print "returning %d items as %d bytes." % (len(res['boundarys']), len(jsondata))
            return jsondata
    except Exception as e:
        msg = "Failed to parse file" + e.message
        print(msg)
        return msg
    return "You missed a field."


# def check_user_credentials(username, password):
#     return username in allowed and allowed[username] == password
#
# @app.route('/login')
# def login():
#     username = request.forms.get('username')
#     password = request.forms.get('password')
#     if check_user_credentials(username, password):
#         response.set_cookie("account", username, secret=COOKIE_KEY)
#         return "Welcome %s! You are now logged in." % username
#     else:
#         return "Login failed."
#
# @app.route('/logout')
# def login():
#     username = request.forms.get('username')
#     password = request.forms.get('password')
#     if check_user_credentials(username, password):
#         response.delete_cookie("account", username, secret=COOKIE_KEY)
#         return "Welcome %s! You are now logged out." % username
#     else:
#         return "Already logged out."



### Setup Argument Parser
argparser = argparse.ArgumentParser(description='Run LasaurApp.', prog='lasaurapp')
argparser.add_argument('port', metavar='serial_port', nargs='?', default=False,
                       help='serial port to the Lasersaur')
argparser.add_argument('-v', '--version', action='version', version='%(prog)s ' + VERSION)
argparser.add_argument('-p', '--public', dest='host_on_all_interfaces', action='store_true',
                       default=False, help='bind to all network devices (default: bind to 127.0.0.1)')
argparser.add_argument('-f', '--flash', dest='flash', action='store_true',
                       default=False, help='flash Arduino with LasaurGrbl firmware')
argparser.add_argument('-b', '--build', dest='build_flash', action='store_true',
                       default=False, help='build and flash from firmware/src')
argparser.add_argument('-l', '--list', dest='list_serial_devices', action='store_true',
                       default=False, help='list all serial devices currently connected')
argparser.add_argument('-d', '--debug', dest='debug', action='store_true',
                       default=False, help='print more verbose for debugging')
argparser.add_argument('--beaglebone', dest='beaglebone', action='store_true',
                       default=False, help='use this for running on beaglebone')
argparser.add_argument('--raspberrypi', dest='raspberrypi', action='store_true',
                       default=False, help='use this for running on Raspberry Pi')
argparser.add_argument('--dummy', dest='dummy', action='store_true',
                       default=False, help='use this for developing without hardware')
argparser.add_argument('-m', '--match', dest='match',
                       default=GUESS_PREFIX, help='match serial device with this string')
args = argparser.parse_args()

print("LasaurApp " + VERSION)

if args.dummy or config.get("dummy_mode", False):
    print("starting in Dummy Mode")
    setDummyMode()
elif args.beaglebone:
    HARDWARE = 'beaglebone'
    NETWORK_PORT = 80
    SERIAL_PORT = "/dev/ttyO1"

    ### if running on beaglebone, setup (pin muxing) and use UART1
    # for details see: http://www.nathandumont.com/node/250
    if os.path.exists("/sys/kernel/debug/omap_mux/uart1_txd"):
        # echo 0 > /sys/kernel/debug/omap_mux/uart1_txd
        fw = file("/sys/kernel/debug/omap_mux/uart1_txd", "w")
        fw.write("%X" % (0))
        fw.close()
        # echo 20 > /sys/kernel/debug/omap_mux/uart1_rxd
        fw = file("/sys/kernel/debug/omap_mux/uart1_rxd", "w")
        fw.write("%X" % ((1 << 5) | 0))
        fw.close()

    ### if running on BBB/Ubuntu 14.04, setup pin muxing UART1
    pin24list = glob.glob("/sys/devices/ocp.*/P9_24_pinmux.*/state")
    for pin24 in pin24list:
        os.system("echo uart > %s" % (pin24))

    pin26list = glob.glob("/sys/devices/ocp.*/P9_26_pinmux.*/state")
    for pin26 in pin26list:
        os.system("echo uart > %s" % (pin26))

    ### Set up atmega328 reset control
    # The reset pin is connected to GPIO2_7 (2*32+7 = 71).
    # Setting it to low triggers a reset.
    # echo 71 > /sys/class/gpio/export

    ### if running on BBB/Ubuntu 14.04, setup pin muxing GPIO2_7 (pin 46)
    pin46list = glob.glob("/sys/devices/ocp.*/P8_46_pinmux.*/state")
    for pin46 in pin46list:
        os.system("echo gpio > %s" % (pin46))

    try:
        fw = file("/sys/class/gpio/export", "w")
        fw.write("%d" % (71))
        fw.close()
    except IOError:
        # probably already exported
        pass
    # set the gpio pin to output
    # echo out > /sys/class/gpio/gpio71/direction
    fw = file("/sys/class/gpio/gpio71/direction", "w")
    fw.write("out")
    fw.close()
    # set the gpio pin high
    # echo 1 > /sys/class/gpio/gpio71/value
    fw = file("/sys/class/gpio/gpio71/value", "w")
    fw.write("1")
    fw.flush()
    fw.close()

    ### Set up atmega328 reset control - BeagleBone Black
    # The reset pin is connected to GPIO2_9 (2*32+9 = 73).
    # Setting it to low triggers a reset.
    # echo 73 > /sys/class/gpio/export

    ### if running on BBB/Ubuntu 14.04, setup pin muxing GPIO2_9 (pin 44)
    pin44list = glob.glob("/sys/devices/ocp.*/P8_44_pinmux.*/state")
    for pin44 in pin44list:
        os.system("echo gpio > %s" % (pin44))

    try:
        fw = file("/sys/class/gpio/export", "w")
        fw.write("%d" % (73))
        fw.close()
    except IOError:
        # probably already exported
        pass
    # set the gpio pin to output
    # echo out > /sys/class/gpio/gpio73/direction
    fw = file("/sys/class/gpio/gpio73/direction", "w")
    fw.write("out")
    fw.close()
    # set the gpio pin high
    # echo 1 > /sys/class/gpio/gpio73/value
    fw = file("/sys/class/gpio/gpio73/value", "w")
    fw.write("1")
    fw.flush()
    fw.close()

    ### read stepper driver configure pin GPIO2_12 (2*32+12 = 76).
    # Low means Geckos, high means SMC11s

    ### if running on BBB/Ubuntu 14.04, setup pin muxing GPIO2_12 (pin 39)
    pin39list = glob.glob("/sys/devices/ocp.*/P8_39_pinmux.*/state")
    for pin39 in pin39list:
        os.system("echo gpio > %s" % (pin39))

    try:
        fw = file("/sys/class/gpio/export", "w")
        fw.write("%d" % (76))
        fw.close()
    except IOError:
        # probably already exported
        pass
    # set the gpio pin to input
    fw = file("/sys/class/gpio/gpio76/direction", "w")
    fw.write("in")
    fw.close()
    # set the gpio pin high
    fw = file("/sys/class/gpio/gpio76/value", "r")
    ret = fw.read()
    fw.close()
    print("Stepper driver configure pin is: " + str(ret))

elif args.raspberrypi:
    HARDWARE = 'raspberrypi'
    NETWORK_PORT = 80
    SERIAL_PORT = "/dev/ttyAMA0"
    import RPi.GPIO as GPIO

    # GPIO.setwarnings(False) # surpress warnings
    GPIO.setmode(GPIO.BCM)  # use chip pin number
    pinSense = 7
    pinReset = 2
    pinExt1 = 3
    pinExt2 = 4
    pinExt3 = 17
    pinTX = 14
    pinRX = 15
    # read sens pin
    GPIO.setup(pinSense, GPIO.IN)
    isSMC11 = GPIO.input(pinSense)
    # atmega reset pin
    GPIO.setup(pinReset, GPIO.OUT)
    GPIO.output(pinReset, GPIO.HIGH)
    # no need to setup the serial pins
    # although /boot/cmdline.txt and /etc/inittab needs
    # to be edited to deactivate the serial terminal login
    # (basically anything related to ttyAMA0)

if args.list_serial_devices:
    SerialManager.list_devices(BITSPERSECOND)
else:
    if not SERIAL_PORT:
        if args.port:
            # (1) get the serial device from the argument list
            SERIAL_PORT = args.port
            print("Using serial device '" + SERIAL_PORT + "' from command line.")

        elif config.get("serial_port", False):
            # (2) get the serial device from the config file
            SERIAL_PORT = config.get("serial_port", False)
            print("Using serial device '" + SERIAL_PORT + "' from '" + CONFIG_FILE + "'.")

        elif args.match:
            GUESS_PREFIX = args.match
            SERIAL_PORT = SerialManager.match_device(GUESS_PREFIX, BITSPERSECOND)
            if SERIAL_PORT:
                print("Using serial device '" + str(SERIAL_PORT))
                if os.name == 'posix':
                    # not for windows for now
                    print("(first device to match: " + args.match + ")")

        else:
            SERIAL_PORT = SerialManager.match_device(GUESS_PREFIX, BITSPERSECOND)
            if SERIAL_PORT:
                print("Using serial device '" + str(SERIAL_PORT) + "' by best guess.")
            else:
                print("-----------------------------------------------------------------------------")
                print("WARNING: LasaurApp doesn't know what serial device to connect to!")
                print("Make sure the Lasersaur hardware is connectd to the USB interface.")
                if os.name == 'nt':
                    print("ON WINDOWS: You will also need to setup the virtual com port.")
                    print("See 'Installing Drivers': http://arduino.cc/en/Guide/Windows")
                print("-----------------------------------------------------------------------------")

    # run
    if args.debug:
        debug(True)
        if hasattr(sys, "_MEIPASS"):
            print("Data root is: " + sys._MEIPASS)
    if args.flash:
        return_code = flash_upload(SERIAL_PORT, resources_dir(), FIRMWARE, HARDWARE)
        if return_code == 0:
            print("SUCCESS: Arduino appears to be flashed.")
        else:
            print("ERROR: Failed to flash Arduino.")
    elif args.build_flash:
        # build
        buildname = "LasaurGrbl_from_src"
        firmware_dir = os.path.join(resources_dir(), 'firmware')
        source_dir = os.path.join(resources_dir(), 'firmware', 'src')
        return_code = build_firmware(source_dir, firmware_dir, buildname)
        if return_code != 0:
            print(ret)
        else:
            print("SUCCESS: firmware built.")
            # flash
            return_code = flash_upload(SERIAL_PORT, resources_dir(), FIRMWARE, HARDWARE)
            if return_code == 0:
                print("SUCCESS: Arduino appears to be flashed.")
            else:
                print("ERROR: Failed to flash Arduino.")
    else:
        if args.host_on_all_interfaces or config.get("public", False):
            run_with_callback('', NETWORK_PORT)
        else:
            run_with_callback('127.0.0.1', NETWORK_PORT)
