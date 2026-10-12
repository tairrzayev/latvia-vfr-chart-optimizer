// Render regions of page 1 of a PDF with CoreGraphics (the engine behind Preview, Safari and iOS).
//
// usage: cgrender file.pdf scale outdir x,y,w,h [x,y,w,h ...]
//   Each region (PDF points, origin bottom-left) is written to outdir/<index>.rgba as raw
//   8-bit RGBA rows, top row first, on a white background. Prints one line per region:
//   "<index> <width> <height> <seconds>".
import CoreGraphics
import Foundation

let args = CommandLine.arguments
guard args.count >= 5, let scale = Double(args[2]) else {
    FileHandle.standardError.write("usage: cgrender file.pdf scale outdir x,y,w,h ...\n".data(using: .utf8)!)
    exit(2)
}
guard let doc = CGPDFDocument(URL(fileURLWithPath: args[1]) as CFURL), let page = doc.page(at: 1) else {
    FileHandle.standardError.write("cannot open \(args[1])\n".data(using: .utf8)!)
    exit(1)
}
let outdir = args[3]
for (i, spec) in args[4...].enumerated() {
    let v = spec.split(separator: ",").compactMap { Double($0) }
    guard v.count == 4 else { fatalError("bad region \(spec)") }
    let region = CGRect(x: v[0], y: v[1], width: v[2], height: v[3])
    let W = Int((region.width * scale).rounded()), H = Int((region.height * scale).rounded())
    guard let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: W * 4,
                              space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { fatalError("context") }
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
    ctx.scaleBy(x: CGFloat(scale), y: CGFloat(scale))
    ctx.translateBy(x: -region.origin.x, y: -region.origin.y)
    ctx.clip(to: region)
    let t0 = Date()
    ctx.drawPDFPage(page)
    let dt = Date().timeIntervalSince(t0)
    let data = Data(bytes: ctx.data!, count: W * H * 4)
    try! data.write(to: URL(fileURLWithPath: "\(outdir)/\(i).rgba"))
    print("\(i) \(W) \(H) \(String(format: "%.3f", dt))")
}
