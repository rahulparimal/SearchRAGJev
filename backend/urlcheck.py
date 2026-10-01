"""Provider URL validation (admin-settable endpoints receive API keys, so a bad URL could exfiltrate them). http/https only, no userinfo,
no link-local / cloud-metadata hosts, optional PROVIDER_URL_ALLOWLIST (comma-separated hosts or .suffixes). Private/loopback hosts stay
allowed (local models, docker service names). Hostnames are not resolved here (DNS-rebinding is out of scope; use the allowlist)."""
import ipaddress,os
from urllib.parse import urlparse
BLOCKED_NAMES={'metadata','metadata.google.internal','instance-data','instance-data.ec2.internal','metadata.azure.com'}
def validate_provider_url(url,allowlist=None):
    """-> normalised url (no trailing slash). Raises ValueError with a safe message."""
    u=urlparse((url or '').strip())
    if u.scheme not in ('http','https'):raise ValueError('URL must use http or https')
    if not u.hostname:raise ValueError('URL must include a host')
    if u.username is not None or u.password is not None or '@' in u.netloc:raise ValueError('URL must not contain credentials')
    h=u.hostname.lower().rstrip('.')
    if h in BLOCKED_NAMES:raise ValueError('URL host is not allowed')
    try:ip=ipaddress.ip_address(h.split('%')[0])
    except ValueError:ip=None
    if ip is not None:
        if getattr(ip,'ipv4_mapped',None):ip=ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or str(ip)=='100.100.100.200':raise ValueError('URL host is not allowed (link-local/metadata address)')
    al=[x.strip().lower() for x in (os.getenv('PROVIDER_URL_ALLOWLIST','') if allowlist is None else allowlist).split(',') if x.strip()]
    if al and not any(h==x.lstrip('.') or (x.startswith('.') and h.endswith(x)) or h.endswith('.'+x.lstrip('.')) for x in al):raise ValueError('URL host is not in PROVIDER_URL_ALLOWLIST')
    return url.strip().rstrip('/')
def safe_provider_url(url,allowlist=None):
    """validated url or '' (callers treat '' as "provider not configured")."""
    try:return validate_provider_url(url,allowlist) if url else ''
    except ValueError:return ''
