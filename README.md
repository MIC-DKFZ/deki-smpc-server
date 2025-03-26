# deki-smpc-server



## Redis data model

"clients:registered" set contains client_name. All unique registered clients.
"clients:info:{client_name}" hash is a dict having some client info. You can use the registration_data.client_name from the "clients:registered" set to access it. it has 