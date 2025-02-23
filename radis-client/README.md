# RADIS Client

## About

RADIS Client is the official Python client of [RADIS (Radiology Report Archive and Discovery System)](https://github.com/openradx/adit).

## Usage

### Prerequisites

- Generate an API token in your RADIS profile.
- Make sure you have the permissions to access the RADIS API.
- Also make sure you have the permissions for the resources you like to access.

### Code

```python
server_url = "https://radis" # The host URL of the RADIS server
auth_token = "my_token" # The authentication token generated in your profile
client = RadisClient(server_url=server_url, auth_token=auth_token)
```

## License

- AGPL 3.0 or later
