//! Python/MiuRead-compatible pure wire construction. Public constants are not credentials.
use crate::*;
use std::io::Read;
const REMAP: [u8; 256] = [
    52, 202, 85, 64, 29, 182, 147, 198, 49, 48, 41, 53, 50, 167, 184, 17, 194, 181, 22, 250, 139,
    177, 36, 164, 16, 144, 4, 233, 8, 248, 59, 138, 156, 140, 68, 249, 188, 92, 105, 226, 161, 218,
    210, 211, 117, 137, 247, 30, 45, 80, 86, 215, 114, 83, 191, 34, 251, 32, 15, 1, 46, 69, 135,
    110, 102, 72, 242, 224, 205, 254, 103, 169, 67, 244, 148, 81, 206, 165, 74, 238, 19, 38, 142,
    204, 170, 51, 20, 93, 14, 57, 187, 207, 145, 43, 129, 77, 234, 153, 236, 26, 44, 133, 197, 217,
    54, 116, 75, 24, 225, 241, 61, 157, 65, 159, 180, 23, 13, 214, 76, 190, 220, 175, 151, 40, 119,
    240, 98, 255, 113, 193, 200, 39, 143, 108, 104, 168, 155, 230, 89, 28, 27, 18, 9, 152, 78, 63,
    6, 55, 0, 186, 31, 10, 25, 47, 201, 213, 208, 87, 73, 111, 253, 37, 228, 97, 12, 66, 203, 150,
    100, 95, 219, 173, 96, 35, 141, 154, 109, 195, 196, 94, 62, 185, 146, 106, 189, 91, 7, 127,
    118, 149, 237, 79, 171, 132, 122, 128, 231, 120, 199, 229, 235, 115, 131, 107, 252, 56, 70,
    125, 71, 101, 179, 82, 99, 58, 5, 209, 239, 163, 166, 222, 158, 60, 2, 174, 178, 123, 160, 246,
    243, 42, 192, 172, 134, 3, 90, 84, 11, 245, 130, 212, 126, 227, 223, 176, 216, 221, 33, 232,
    124, 136, 162, 121, 88, 112, 183, 21,
];
pub const DEFAULT_GUEST: &str = "5ecdcfd7f";
fn rotate(mut v: Vec<u8>) -> Vec<u8> {
    let sum = v.iter().fold(0u8, |a, b| a ^ b);
    let n = sum as usize % 11 % v.len();
    v.rotate_right(n);
    v
}
pub fn sign(parts: &[String]) -> String {
    let mut v = vec![b"5a6f1".to_vec()];
    v.extend(
        parts
            .iter()
            .map(|s| s.bytes().map(|b| REMAP[b as usize]).collect::<Vec<_>>()),
    );
    v.sort();
    let joined: Vec<u8> = v.concat();
    sha(rotate(sha(rotate(joined)).into_bytes()))
}
pub fn random() -> Result<u16> {
    let mut b = [0u8; 2];
    fs::File::open("/dev/urandom")?.read_exact(&mut b)?;
    Ok(u16::from_ne_bytes(b) % 1000)
}
pub fn packet(
    a: &Auth,
    bid: &str,
    hours: &[Value],
    config: &Value,
    feature: &Value,
    ms: i64,
    inner: u16,
    outer: u16,
) -> Result<Value> {
    ensure!(
        inner < 1000 && outer < 1000 && !hours.is_empty() && !bid.is_empty(),
        "invalid request"
    );
    let token = config["token"]
        .as_str()
        .filter(|s| !s.is_empty())
        .context("signing config absent")?;
    let guest = feature["feature"]["guest_token"]
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or(DEFAULT_GUEST);
    let mut total = 0;
    let mut distinct = std::collections::BTreeSet::new();
    let mut parts = vec![
        a.vid.clone(),
        a.device_id.clone(),
        String::new(),
        bid.to_owned(),
        "0".into(),
        "+08:00".into(),
    ];
    for h in hours {
        let start = h["startOfHour"].as_i64().context("invalid hour")?;
        let secs = h["readingTime"].as_i64().context("invalid seconds")?;
        ensure!(
            start > 0
                && start % 3600 == 0
                && secs > 0
                && secs <= 3600
                && h["timeZone"] == "+08:00"
                && h["ttsTime"] == 0
                && distinct.insert(start),
            "invalid hour bucket"
        );
        total += secs;
    }
    ensure!(total > 0 && total <= 86400, "unsupported batch duration");
    parts.extend([total.to_string(), "0".into()]);
    for h in hours {
        parts.push(format!(
            "{}_+08:00_{}_0",
            h["startOfHour"].as_i64().unwrap(),
            h["readingTime"].as_i64().unwrap()
        ));
    }
    let timestamp = ms / 1000;
    let signature = sign(&[
        guest.to_owned(),
        inner.to_string(),
        timestamp.to_string(),
        parts.join("_"),
    ]);
    let book = json!({"bookId":bid,"bookVersion":0,"appId":"","deviceId":a.device_id,"installId":"","summary":"","progress":0,"currentProgress":0,"autoTime":0,"isLecture":0,"isResendReadingInfo":1,"lectureTime":0,"lectureTextTime":0,"novalTime":0,"ttsTime":0,"finish":false,"risk":0,"readingTime":total,"recordCreateTimeZone":"+08:00","timestamp":timestamp,"random":inner,"hours":hours,"signature":signature});
    Ok(
        json!({"books":[book],"timestamp":ms,"random":outer,"signature":sha(format!("{ms}{token}{outer}"))}),
    )
}
