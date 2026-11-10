import AppKit
import CoreGraphics
import Foundation
import ScreenCaptureKit

private let appTitle = "咕顾谷录屏检测"
private let permissionHelp = "请在「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」允许当前这份检测工具，再完全退出并重新打开。"

private struct CheckFailure: Error {
    let code: Int
    let help: String
}

@MainActor
private final class CaptureCheckDelegate: NSObject, NSApplicationDelegate {
    private let displayPicker = NSPopUpButton(frame: .zero, pullsDown: false)
    private let preflightLabel = NSTextField(labelWithString: "")
    private let resultLabel = NSTextField(wrappingLabelWithString: "请选择显示器，然后点击「检测录屏」。")
    private lazy var refreshButton = NSButton(title: "刷新显示器", target: self, action: #selector(refreshDisplays))
    private lazy var permissionButton = NSButton(title: "授权屏幕录制", target: self, action: #selector(requestPermission))
    private lazy var detectButton = NSButton(title: "检测录屏", target: self, action: #selector(detectCapture))
    private lazy var copyButton = NSButton(title: "复制检测结果", target: self, action: #selector(copyReport))
    private var window: NSWindow?
    private var checking = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenu()
        displayPicker.target = self
        displayPicker.action = #selector(displayChanged)
        displayPicker.widthAnchor.constraint(greaterThanOrEqualToConstant: 250).isActive = true

        let title = NSTextField(labelWithString: appTitle)
        title.font = .boldSystemFont(ofSize: 23)
        let introduction = NSTextField(wrappingLabelWithString:
            "此工具独立检测所选显示器能否提供一帧画面。检测结果只包含状态和尺寸，画面不会显示或保存。")
        introduction.textColor = .secondaryLabelColor
        let displayRow = NSStackView(views: [NSTextField(labelWithString: "检测显示器"), displayPicker, refreshButton])
        displayRow.spacing = 8
        let actions = NSStackView(views: [permissionButton, detectButton, copyButton])
        actions.spacing = 8
        let note = NSTextField(wrappingLabelWithString:
            "权限属于当前应用。检测工具通过后，咕顾谷主程序仍需要自己的录屏授权。")
        note.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [title, introduction, displayRow, preflightLabel, actions, resultLabel, note])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 14
        stack.translatesAutoresizingMaskIntoConstraints = false

        let panel = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 390),
                             styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        panel.title = appTitle
        panel.isReleasedWhenClosed = false
        if let content = panel.contentView {
            content.addSubview(stack)
            NSLayoutConstraint.activate([
                stack.topAnchor.constraint(equalTo: content.topAnchor, constant: 24),
                stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 24),
                stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -24),
                stack.bottomAnchor.constraint(lessThanOrEqualTo: content.bottomAnchor, constant: -24),
                introduction.widthAnchor.constraint(equalTo: stack.widthAnchor),
                resultLabel.widthAnchor.constraint(equalTo: stack.widthAnchor),
                note.widthAnchor.constraint(equalTo: stack.widthAnchor),
            ])
        }
        window = panel
        refreshDisplays()
        panel.center()
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        updatePreflight()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func installMenu() {
        let menu = NSMenu()
        let applicationItem = NSMenuItem()
        let applicationMenu = NSMenu()
        applicationMenu.addItem(withTitle: "退出\(appTitle)", action: #selector(NSApplication.terminate(_:)),
                                keyEquivalent: "q")
        applicationItem.submenu = applicationMenu
        menu.addItem(applicationItem)
        NSApp.mainMenu = menu
    }

    private var selectedDisplayID: CGDirectDisplayID? {
        (displayPicker.selectedItem?.representedObject as? NSNumber)?.uint32Value
    }

    @objc private func refreshDisplays() {
        let previous = selectedDisplayID
        let mainDisplay = CGMainDisplayID()
        let screens = NSScreen.screens.compactMap { screen -> (CGDirectDisplayID, NSScreen)? in
            guard let number = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber else {
                return nil
            }
            return (number.uint32Value, screen)
        }.sorted { first, second in
            if (first.0 == mainDisplay) != (second.0 == mainDisplay) {
                return first.0 == mainDisplay
            }
            return first.0 < second.0
        }

        displayPicker.removeAllItems()
        for (index, entry) in screens.enumerated() {
            let (displayID, screen) = entry
            let size = screen.frame.size
            let label = displayID == mainDisplay ? "主显示器" : "显示器 \(index + 1)"
            displayPicker.addItem(withTitle: "\(label) · \(screen.localizedName) · \(Int(size.width)) × \(Int(size.height))")
            displayPicker.lastItem?.representedObject = NSNumber(value: displayID)
        }
        if let index = screens.firstIndex(where: { $0.0 == previous }) {
            displayPicker.selectItem(at: index)
        } else if !screens.isEmpty {
            displayPicker.selectItem(at: 0)
        }
        if screens.isEmpty {
            resultLabel.stringValue = "未找到可用显示器。请确认显示器在线，然后刷新。"
        } else if previous != selectedDisplayID {
            displayChanged()
        }
        updateControls()
        updatePreflight()
    }

    @objc private func displayChanged() {
        resultLabel.stringValue = "请点击「检测录屏」，检查所选显示器。"
    }

    private func updateControls() {
        displayPicker.isEnabled = !checking && selectedDisplayID != nil
        refreshButton.isEnabled = !checking
        permissionButton.isEnabled = !checking
        detectButton.isEnabled = !checking && selectedDisplayID != nil
        copyButton.isEnabled = !checking
    }

    private func updatePreflight() {
        let status = CGPreflightScreenCaptureAccess() ? "系统预检已确认授权" : "系统预检尚未确认授权"
        preflightLabel.stringValue = "\(status)，可用「检测录屏」查看实际结果。"
    }

    @objc private func requestPermission() {
        let granted = CGRequestScreenCaptureAccess()
        updatePreflight()
        resultLabel.stringValue = granted ? "系统已确认授权。请点击「检测录屏」验证实际捕获。" : permissionHelp
    }

    @objc private func detectCapture() {
        guard !checking, let displayID = selectedDisplayID else { return }
        checking = true
        updateControls()
        resultLabel.stringValue = "正在检测所选显示器……"
        Task { [weak self] in
            await self?.checkDisplay(displayID)
        }
    }

    private func checkDisplay(_ displayID: CGDirectDisplayID) async {
        defer {
            checking = false
            updateControls()
            updatePreflight()
        }

        var stage = "确认所选显示器"
        do {
            let originalBounds = CGDisplayBounds(displayID)
            guard CGDisplayIsActive(displayID) != 0, !originalBounds.isEmpty else {
                throw CheckFailure(code: 1, help: "所选显示器已不可用。请刷新显示器列表，再选择并检测。")
            }

            stage = "查询系统可捕获的显示器"
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
            guard let display = content.displays.first(where: { $0.displayID == displayID }) else {
                throw CheckFailure(code: 2, help: "系统未提供所选显示器。请确认显示器在线，再刷新并检测。")
            }

            stage = "准备显示器画面"
            let ownPID = ProcessInfo.processInfo.processIdentifier
            let ownApplication = content.applications.first(where: { $0.processID == ownPID })
                ?? content.windows.compactMap(\.owningApplication).first(where: { $0.processID == ownPID })
            let filter: SCContentFilter
            let exclusion: String
            if let ownApplication {
                filter = SCContentFilter(display: display, excludingApplications: [ownApplication], exceptingWindows: [])
                exclusion = "已排除检测工具自身。"
            } else {
                let ownWindowIDs = Set(NSApp.windows.compactMap { CGWindowID(exactly: $0.windowNumber) })
                let ownWindows = content.windows.filter { ownWindowIDs.contains($0.windowID) }
                filter = SCContentFilter(display: display, excludingWindows: ownWindows)
                exclusion = ownWindows.isEmpty ? "系统未提供检测工具的窗口信息。" : "已排除检测工具的窗口。"
            }

            let pointSize = filter.contentRect.size
            let scale = CGFloat(filter.pointPixelScale)
            let pixelWidth = (pointSize.width * scale).rounded()
            let pixelHeight = (pointSize.height * scale).rounded()
            guard pixelWidth.isFinite, pixelHeight.isFinite, pixelWidth >= 1, pixelHeight >= 1,
                  pixelWidth <= 16_384, pixelHeight <= 16_384, pixelWidth * pixelHeight <= 32_000_000 else {
                throw CheckFailure(code: 3, help: "系统返回的画面尺寸无效或过大。请调整显示器分辨率后再检测。")
            }

            let configuration = SCStreamConfiguration()
            configuration.width = Int(pixelWidth)
            configuration.height = Int(pixelHeight)
            configuration.showsCursor = false
            configuration.capturesAudio = false
            if #available(macOS 15.0, *) {
                configuration.captureMicrophone = false
            }

            stage = "获取一帧显示器画面"
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
            stage = "核对画面和显示器"
            guard CGDisplayIsActive(displayID) != 0, CGDisplayBounds(displayID) == originalBounds else {
                throw CheckFailure(code: 4, help: "检测期间显示器发生变化。请刷新后再检测。")
            }
            guard image.width == configuration.width, image.height == configuration.height else {
                throw CheckFailure(code: 5, help: "系统返回了 \(image.width) × \(image.height) 像素，与请求尺寸不一致。请重新检测。")
            }
            resultLabel.stringValue = "检测通过，已取得 \(image.width) × \(image.height) 像素的一帧画面。\n\(exclusion)"
        } catch let failure as CheckFailure {
            showFailure(stage: stage, domain: "GuguguCaptureCheck", code: failure.code, help: failure.help)
        } catch {
            let failure = error as NSError
            let help: String
            if failure.domain == SCStreamError.errorDomain && failure.code == SCStreamError.userDeclined.rawValue {
                help = permissionHelp
            } else if failure.domain == SCStreamError.errorDomain && failure.code == SCStreamError.missingEntitlements.rawValue {
                help = "系统拒绝了此版本的录屏配置。请复制检测结果反馈。"
            } else {
                help = "请确认所选显示器仍在线，然后重新检测。若仍失败，请复制检测结果反馈。"
            }
            showFailure(stage: stage, domain: failure.domain, code: failure.code, help: help)
        }
    }

    private func showFailure(stage: String, domain: String, code: Int, help: String) {
        resultLabel.stringValue = "\(stage)失败（\(domain) · \(code)）\n\(help)"
    }

    @objc private func copyReport() {
        let report = [appTitle, "macOS \(ProcessInfo.processInfo.operatingSystemVersionString) · arm64",
                      displayPicker.titleOfSelectedItem ?? "无显示器", preflightLabel.stringValue,
                      resultLabel.stringValue].joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(report, forType: .string)
    }
}

if CommandLine.arguments.contains("--smoke-test") {
    print("GuguguCaptureCheck smoke test passed")
} else {
    MainActor.assumeIsolated {
        let application = NSApplication.shared
        let delegate = CaptureCheckDelegate()
        application.setActivationPolicy(.regular)
        application.delegate = delegate
        withExtendedLifetime(delegate) {
            application.run()
        }
    }
}
