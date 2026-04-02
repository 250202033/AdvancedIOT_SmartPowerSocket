import os
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# --- CONFIGURATION ---
CERT_DIR = "./mosquitto/config/certs"
DAYS_VALID = 3650

def load_ca():
    """Load the existing CA certificate and private key from disk."""
    try:
        with open(os.path.join(CERT_DIR, "ca.crt"), "rb") as f:
            ca_cert = x509.load_pem_x509_certificate(f.read())
        with open(os.path.join(CERT_DIR, "ca.key"), "rb") as f:
            ca_key = serialization.load_pem_private_key(f.read(), password=None)
        return ca_cert, ca_key
    except FileNotFoundError:
        print(f"Error: CA files not found in {CERT_DIR}. Run gen_server.py first.")
        exit(1)

def main():
    # 1. Get input from user
    client_id = input("Enter Client Name (e.g., ESP32-Sensor-01): ").strip()
    if not client_id:
        print("Error: Client name cannot be empty.")
        return

    # 2. Load CA credentials
    ca_cert, ca_key = load_ca()

    # 3. Generate a new RSA Private Key for the client
    client_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # 4. Create and Sign the Client Certificate
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, client_id)])
    now = datetime.now(timezone.utc)
    
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        ca_cert.subject
    ).public_key(
        client_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        now
    ).not_valid_after(
        now + timedelta(days=DAYS_VALID)
    ).sign(ca_key, hashes.SHA256())

    # 5. Format to PEM strings
    key_pem = client_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption()
    ).decode('utf-8')

    cert_pem = cert.public_bytes(
        encoding=serialization.Encoding.PEM
    ).decode('utf-8')

    # 6. Output to Terminal
    print("\n" + "="*50)
    print(f" CERTIFICATE AND KEY FOR: {client_id}")
    print("="*50)
    
    print("\n### CLIENT PRIVATE KEY ###")
    print(key_pem)
    
    print("### CLIENT CERTIFICATE ###")
    print(cert_pem)
    
    print("="*50)
    print("Copy the text above (including BEGIN/END lines).")

if __name__ == "__main__":
    main()
