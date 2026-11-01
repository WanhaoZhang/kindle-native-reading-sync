//! Closed route transport, no redirects, no retry of ambiguous writes.
use crate::*;
use std::{sync::Arc, time::Duration};
#[derive(Debug, Clone, PartialEq)]
pub enum NetError {
    Auth,
    Limited,
    Uncertain,
    Invalid,
}
impl std::fmt::Display for NetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "network {:?}", self)
    }
}
impl std::error::Error for NetError {}
pub trait Transport {
    fn request(
        &mut self,
        method: &str,
        path: &str,
        query: &[(&str, String)],
        body: Option<&Value>,
        auth: &Auth,
    ) -> std::result::Result<Value, NetError>;
}
pub struct Live {
    agent: ureq::Agent,
}
impl Default for Live {
    fn default() -> Self {
        let tls = ureq::tls::TlsConfig::builder()
            .provider(ureq::tls::TlsProvider::Rustls)
            .unversioned_rustls_crypto_provider(Arc::new(rustls_rustcrypto::provider()))
            .build();
        let config = ureq::Agent::config_builder()
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_global(Some(Duration::from_secs(20)))
            .timeout_connect(Some(Duration::from_secs(5)))
            .tls_config(tls)
            .build();
        Self {
            agent: ureq::Agent::new_with_config(config),
        }
    }
}
impl Transport for Live {
    fn request(
        &mut self,
        method: &str,
        path: &str,
        query: &[(&str, String)],
        body: Option<&Value>,
        a: &Auth,
    ) -> std::result::Result<Value, NetError> {
        if !matches!(
            (method, path),
            (
                "GET",
                "/book/getProgress" | "/config" | "/feature" | "/readdata/detail"
            ) | ("POST", "/login" | "/book/batchUploadProgress")
        ) {
            return Err(NetError::Invalid);
        }
        let mut url = ureq::http::Uri::try_from(format!("https://i.weread.qq.com{path}"))
            .map_err(|_| NetError::Invalid)?
            .to_string();
        if !query.is_empty() {
            let mut q = Vec::new();
            for (k, v) in query {
                if !k.bytes().all(|c| c.is_ascii_alphanumeric())
                    || !v
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
                {
                    return Err(NetError::Invalid);
                }
                q.push(format!("{k}={v}"));
            }
            url.push('?');
            url.push_str(&q.join("&"));
        }
        // Sensitive values are never part of an error string or command line.
        let headers = [
            ("baseapi", "30"),
            ("appver", "2.1.2.10245900"),
            ("basever", "2.1.2.10245900"),
            ("osver", "11"),
            ("channelId", "900"),
            (
                "User-Agent",
                "WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 (Linux; U; Android 11; BOOX Build/onyx)",
            ),
        ];
        let result = if method == "GET" {
            let mut req = self
                .agent
                .get(&url)
                .header("vid", &a.vid)
                .header("accessToken", &a.access_token);
            for (k, v) in headers {
                req = req.header(k, v);
            }
            req.call()
        } else {
            let mut req = self
                .agent
                .post(&url)
                .header("vid", &a.vid)
                .header("accessToken", &a.access_token)
                .header("Content-Type", "application/json");
            for (k, v) in headers {
                req = req.header(k, v);
            }
            req.send(body.ok_or(NetError::Invalid)?.to_string().as_bytes())
        };
        let mut response = result.map_err(|_| NetError::Uncertain)?;
        let status = response.status().as_u16();
        if status == 401 {
            return Err(NetError::Auth);
        }
        if status == 429 {
            return Err(NetError::Limited);
        }
        if status != 200 {
            return Err(NetError::Uncertain);
        }
        let text = response
            .body_mut()
            .with_config()
            .limit(2 * 1024 * 1024)
            .read_to_string()
            .map_err(|_| NetError::Uncertain)?;
        let v: Value = serde_json::from_str(&text).map_err(|_| NetError::Invalid)?;
        if let Some(code) = v.get("errCode").or_else(|| v.get("errcode")) {
            let code = code
                .as_i64()
                .or_else(|| code.as_str().and_then(|s| s.parse().ok()));
            if code != Some(0) {
                if matches!(code, Some(-2011 | -2012 | -2041)) {
                    return Err(NetError::Auth);
                }
                return Err(NetError::Uncertain);
            }
        }
        Ok(v)
    }
}
pub fn refresh<T: Transport>(transport: &mut T, root: &Path, a: &Auth) -> Result<Auth> {
    let timestamp = chrono::Utc::now().timestamp_millis();
    let random = crate::protocol::random()?;
    let body = json!({"deviceId":a.device_id,"deviceName":"BOOX","deviceType":3,"timestamp":timestamp,"random":random,"trackId":"","signature":sha(format!("{timestamp}{}{random}",a.device_id)),"refreshToken":a.refresh_token,"inBackground":0,"kickType":1,"refCgi":""});
    let v = transport.request("POST", "/login", &[], Some(&body), a)?;
    let id = v["vid"]
        .as_str()
        .map(str::to_owned)
        .or_else(|| v["vid"].as_u64().map(|v| v.to_string()))
        .context("refresh identity absent")?;
    ensure!(id == a.vid, "refresh account mismatch");
    let access = v["accessToken"]
        .as_str()
        .filter(|s| !s.is_empty())
        .context("refresh incomplete")?;
    let refreshed = Auth {
        vid: a.vid.clone(),
        device_id: a.device_id.clone(),
        access_token: access.to_owned(),
        refresh_token: v["refreshToken"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or(&a.refresh_token)
            .to_owned(),
    };
    atomic_json(&root.join("auth.json"), &serde_json::to_value(&refreshed)?)?;
    Ok(refreshed)
}

/// Refresh at most once for authenticated GET preflight; never used after a time POST.
pub fn preflight<T: Transport>(
    t: &mut T,
    root: &Path,
    a: &mut Auth,
    refreshed: &mut bool,
    path: &str,
    query: &[(&str, String)],
) -> Result<Value> {
    check_cancel()?;
    let mut value = t.request("GET", path, query, None, a);
    if value == Err(NetError::Auth) && !*refreshed {
        check_cancel()?;
        *refreshed = true;
        *a = refresh(t, root, a)?;
        value = t.request("GET", path, query, None, a);
    }
    Ok(value?)
}

impl Live {
    /// The only anonymous online self-test: fixed HTTPS GET, no credentials read or sent.
    pub fn anonymous_probe(&mut self) -> Result<Value> {
        let mut response = self
            .agent
            .get("https://i.weread.qq.com/config")
            .header("baseapi", "30")
            .header("appver", "2.1.2.10245900")
            .header("User-Agent", "WeRead/2.1.2")
            .call()
            .map_err(|_| NetError::Uncertain)?;
        let status = response.status().as_u16();
        let text = response
            .body_mut()
            .with_config()
            .limit(2 * 1024 * 1024)
            .read_to_string()
            .map_err(|_| NetError::Uncertain)?;
        let json = serde_json::from_str::<Value>(&text).is_ok();
        Ok(
            json!({"state":"completed","http_status":status,"json_response":json,"https_certificate_validated":true,"credentials_sent":false,"network_writes":false}),
        )
    }
}
