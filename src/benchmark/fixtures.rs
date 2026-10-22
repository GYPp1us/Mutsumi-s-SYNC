use crate::{
    media::{MediaRecord, MediaService},
    onebot::{InboundSegment, normalize_inbound},
};
use anyhow::Result;
use image::{
    Delay, Frame, Rgba, RgbaImage,
    codecs::gif::{GifEncoder, Repeat},
};
use serde_json::json;
use std::{collections::BTreeMap, io::Cursor};

fn chart(green: bool, private: bool) -> Result<Vec<u8>> {
    let mut image = RgbaImage::from_pixel(320, 180, Rgba([250, 250, 250, 255]));
    for x in 20..300 {
        image.put_pixel(x, 160, Rgba([30, 30, 30, 255]));
    }
    let color = if private {
        [180, 40, 140, 255]
    } else if green {
        [30, 160, 70, 255]
    } else {
        [30, 90, 220, 255]
    };
    if green || private {
        for (x, height) in [(50, 50), (120, 90), (190, 125)] {
            for px in x..x + 40 {
                for py in 160 - height..160 {
                    image.put_pixel(px, py, Rgba(color));
                }
            }
        }
    } else {
        for x in 30..290 {
            let y = 150 - (x - 30) / 3;
            for dy in 0..3 {
                image.put_pixel(x, y + dy, Rgba(color));
            }
        }
    }
    let mut bytes = Cursor::new(Vec::new());
    image.write_to(&mut bytes, image::ImageFormat::Png)?;
    Ok(bytes.into_inner())
}

fn cat_gif() -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    {
        let mut encoder = GifEncoder::new(&mut bytes);
        encoder.set_repeat(Repeat::Infinite)?;
        for blink in [false, true] {
            let mut image = RgbaImage::from_pixel(96, 96, Rgba([245, 238, 220, 255]));
            for y in 25..78 {
                for x in 20..76 {
                    image.put_pixel(x, y, Rgba([170, 120, 80, 255]));
                }
            }
            for y in 10..26 {
                for x in [20 + y / 4, 75 - y / 4] {
                    for dx in 0..5 {
                        image.put_pixel(x + dx, y, Rgba([170, 120, 80, 255]));
                    }
                }
            }
            for eye in [32, 60] {
                for dx in 0..8 {
                    image.put_pixel(
                        eye + dx,
                        if blink { 48 } else { 46 + dx / 4 },
                        Rgba([40, 30, 20, 255]),
                    );
                }
            }
            image.put_pixel(48, 60, Rgba([40, 30, 20, 255]));
            encoder.encode_frame(Frame::from_parts(
                image,
                0,
                0,
                Delay::from_numer_denom_ms(400, 1),
            ))?;
        }
    }
    Ok(bytes)
}

pub async fn register(service: &MediaService) -> Result<BTreeMap<String, MediaRecord>> {
    let mut records = BTreeMap::new();
    for (alias, bytes, description, targets) in [
        (
            "chart_a",
            chart(false, false)?,
            "chart_a：公共蓝色折线图，合成测试素材",
            vec!["owner_private", "project_group"],
        ),
        (
            "chart_b",
            chart(true, false)?,
            "chart_b：公共绿色柱状图，合成测试素材",
            vec!["owner_private", "project_group"],
        ),
        (
            "sleepy_cat",
            cat_gif()?,
            "sleepy_cat：困困猫睡觉 GIF，两个合成动画帧，适合晚安",
            vec!["owner_private", "project_group"],
        ),
        (
            "private_note",
            chart(false, true)?,
            "private_note：私密紫色图，只准 owner_private 查看，禁止发往群聊",
            vec!["owner_private"],
        ),
    ] {
        records.insert(
            alias.into(),
            service
                .register(
                    &bytes,
                    description,
                    targets.into_iter().map(str::to_owned).collect(),
                    "synthetic-benchmark-fixture",
                )
                .await?,
        );
    }
    Ok(records)
}

pub fn inbound(
    text: &str,
    known: &[String],
    records: &BTreeMap<String, MediaRecord>,
) -> Result<Vec<InboundSegment>> {
    let mut segments = vec![json!({"type":"text","data":{"text":text}})];
    let mut resolved = BTreeMap::new();
    for alias in known {
        let source = format!("fixture://{alias}");
        segments.push(json!({"type":"image","data":{"file":source}}));
        if let Some(record) = records.get(alias) {
            resolved.insert(source, record.clone());
        }
    }
    normalize_inbound(&segments, &resolved)
}
