[machine]
target_size=[1220,610]
tolerance=0.01
port=/dev/ttyACM0
baud=115200
influx=false
dummy_mode=false
public=false

[web]
cookie_web=secret_key_jkn23489hsdf
company_name=de.fablab
language=de
port=8070

[accounting]
log_file=logs/accounting.csv

[ldap]
use_ldap=true
server=ldap://ldap.fablab-karlsruhe.de
account=cn=admin,dc=fablab-karlsruhe,dc=de
password=A3X9%mI0
use_tls=false
base=dc=fablab-karlsruhe,dc=de
users=ou=users
groups=ou=groups

[sensors]
use=true
port=/dev/ttyACM1
baud=115200