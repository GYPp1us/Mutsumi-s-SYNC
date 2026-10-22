//! One process, one reusable Renderer. The parent owns timeout and process recovery.
use anyhow::{Result, bail};
use serde::{Deserialize, Serialize};
use std::{
    io::{self, BufRead, Read, Write},
    path::{Path, PathBuf},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    id: String,
    markdown: String,
    width: u32,
}

#[derive(Serialize)]
struct Output {
    id: String,
    svg_file: String,
    png_file: String,
    width: u32,
    height: u32,
}

fn render(renderer: &mut md2svg::Renderer, root: &Path, request: Request) -> Result<Output> {
    if request.id.len() != 32 || !request.id.chars().all(|c| c.is_ascii_hexdigit()) {
        bail!("invalid request id")
    }
    if request.markdown.len() > 32 * 1024 || !(256..=1200).contains(&request.width) {
        bail!("render budget exceeded")
    }
    let svg = renderer
        .render(&request.markdown, request.width)
        .map_err(|_| anyhow::anyhow!("md2svg rejected input"))?;
    let options = resvg::usvg::Options::default();
    let tree = resvg::usvg::Tree::from_str(&svg, &options)?;
    let width = tree.size().width().ceil() as u32;
    let height = tree.size().height().ceil() as u32;
    if width == 0 || height == 0 || u64::from(width) * u64::from(height) > 16_000_000 {
        bail!("pixel budget exceeded")
    }
    let mut pixmap = resvg::tiny_skia::Pixmap::new(width, height)
        .ok_or_else(|| anyhow::anyhow!("allocation rejected"))?;
    pixmap.fill(resvg::tiny_skia::Color::WHITE);
    resvg::render(
        &tree,
        resvg::tiny_skia::Transform::identity(),
        &mut pixmap.as_mut(),
    );
    let svg_file = format!("{}.svg", request.id);
    let png_file = format!("{}.png", request.id);
    let png = pixmap.encode_png()?;
    if png.len() > 16 * 1024 * 1024 {
        bail!("PNG size budget exceeded")
    }
    // Filenames are host-generated nonces. Model input cannot select filesystem paths.
    for (name, bytes) in [(&svg_file, svg.as_bytes()), (&png_file, png.as_slice())] {
        let mut f = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(root.join(name))?;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    Ok(Output {
        id: request.id,
        svg_file,
        png_file,
        width,
        height,
    })
}

fn main() -> Result<()> {
    let mut args = std::env::args().skip(1);
    if args.next().as_deref() != Some("--root") {
        bail!("usage: mutsumi-md-worker --root DIRECTORY")
    }
    let root = PathBuf::from(args.next().ok_or_else(|| anyhow::anyhow!("missing root"))?);
    std::fs::create_dir_all(&root)?;
    let root = root.canonicalize()?;
    let mut renderer = md2svg::Renderer::new();
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let mut output = io::stdout().lock();
    loop {
        let mut line = Vec::new();
        let read = std::io::Read::by_ref(&mut input)
            .take(64 * 1024 + 1)
            .read_until(b'\n', &mut line)?;
        if read == 0 {
            break;
        }
        if line.len() > 64 * 1024 {
            bail!("request line exceeds budget")
        }
        let response = match serde_json::from_slice::<Request>(&line) {
            Ok(request) => match render(&mut renderer, &root, request) {
                Ok(result) => serde_json::json!({"ok":true,"output":result}),
                Err(_) => {
                    serde_json::json!({"ok":false,"error":"render input unsupported or resource limit reached"})
                }
            },
            Err(_) => serde_json::json!({"ok":false,"error":"invalid request"}),
        };
        writeln!(output, "{response}")?;
        output.flush()?;
    }
    Ok(())
}
