Public key goes here. Generate with:
  openssl ecparam -name prime256v1 -genkey -noout -out tesla_private.pem
  openssl ec -in tesla_private.pem -pubout -out com.tesla.3p.public-key.pem
Then drop the .pem next to this README.
