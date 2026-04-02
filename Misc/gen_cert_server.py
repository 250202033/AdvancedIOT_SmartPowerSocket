import os
import socket
import ipaddress
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# --- CONFIGURATION ---
CERT_DIR = "./mosquitto/config/certs"
DAYS_VALID = 3650

def get_host_info():
    hostname = socket.gethostname()
    fqdn = socket.getfqdn()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return hostname, fqdn, ip

def generate_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)

def save_file(filename, data):
    os.makedirs(CERT_DIR, exist_ok=True)
    with open(os.path.join(CERT_DIR, filename), 'wb') as f:
        f.write(data)

def sign_cert(subject_name, subject_key, issuer_cert, issuer_key, is_ca=False, sans=None):
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_name)])
    issuer = issuer_cert.subject if issuer_cert else subject
    now = datetime.now(timezone.utc)

    builder = x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(
        subject_key.public_key()).serial_number(x509.random_serial_number()
    ).not_valid_before(now).not_valid_after(now + timedelta(days=DAYS_VALID))

    if is_ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)

    if sans:
        san_list = []
        for san in sans:
            try:
                san_list.append(x509.IPAddress(ipaddress.ip_address(san)))
            except ValueError:
                san_list.append(x509.DNSName(san))
        builder = builder.add_extension(x509.SubjectAlternativeName(san_list), critical=False)

    return builder.sign(issuer_key, hashes.SHA256())

def main():
    hostname, fqdn, ip = get_host_info()
    common_name = fqdn if fqdn and fqdn != 'localhost' else hostname
    
    print(f"--- Initializing CA and Server Certs ---")
    
    # 1. Generate CA
    ca_key = generate_key()
    ca_cert = sign_cert("MyLocalCA", ca_key, None, ca_key, is_ca=True)
    
    # 2. Generate Server Cert
    server_key = generate_key()
    server_sans = list(set(filter(None, [hostname, fqdn, ip, "localhost", "127.0.0.1"])))
    server_cert = sign_cert(common_name, server_key, ca_cert, ca_key, sans=server_sans)

    # Save Files
    save_file("ca.key", ca_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    save_file("ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM))
    save_file("server.key", server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    save_file("server.crt", server_cert.public_bytes(serialization.Encoding.PEM))
    
    print(f"Done! CA and Server certs saved to {CERT_DIR}")
    print("Keep 'ca.key' private; it is needed to sign client certs.")

if __name__ == "__main__":
    main()
