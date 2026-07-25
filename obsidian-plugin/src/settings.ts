import { App, PluginSettingTab, Setting } from "obsidian";
import type KrunRagPlugin from "./main";

export interface KrunRagSettings {
  serverUrl: string;
  topK: number;
  rerankerEnabled: boolean;
  noAnalyze: boolean;
  maxChunksPerFile: number;
  sendActiveNote: boolean;
  /** Path to run_api.bat — used by the status-pill "서버 시작" menu (desktop only). */
  serverScriptPath: string;
}

export const DEFAULT_SETTINGS: KrunRagSettings = {
  serverUrl: "http://127.0.0.1:8765",
  topK: 8,
  rerankerEnabled: false,
  noAnalyze: false,
  maxChunksPerFile: 2,
  sendActiveNote: false,
  serverScriptPath: "C:\\Users\\anton\\Documents\\Claude AI_Personal\\krun-rag-chatbot\\scripts\\run_api.bat",
};

export class KrunRagSettingTab extends PluginSettingTab {
  plugin: KrunRagPlugin;

  constructor(app: App, plugin: KrunRagPlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "KRUN RAG settings" });

    new Setting(containerEl)
      .setName("Server URL")
      .setDesc("FastAPI server endpoint. Default localhost:8765.")
      .addText((t) =>
        t
          .setPlaceholder("http://127.0.0.1:8765")
          .setValue(this.plugin.settings.serverUrl)
          .onChange(async (v) => {
            this.plugin.settings.serverUrl = v.trim().replace(/\/$/, "");
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("서버 실행 파일 (run_api.bat)")
      .setDesc("상태 표시를 클릭해 '서버 시작'을 누르면 이 .bat 을 실행합니다. (데스크톱 전용)")
      .addText((t) =>
        t
          .setPlaceholder("C:\\...\\krun-rag-chatbot\\scripts\\run_api.bat")
          .setValue(this.plugin.settings.serverScriptPath)
          .onChange(async (v) => {
            this.plugin.settings.serverScriptPath = v.trim();
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Top-K citations")
      .setDesc("Max number of chunks sent to Claude (default 8).")
      .addSlider((s) =>
        s
          .setLimits(3, 20, 1)
          .setValue(this.plugin.settings.topK)
          .setDynamicTooltip()
          .onChange(async (v) => {
            this.plugin.settings.topK = v;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Max chunks per file")
      .setDesc("Diversification: cap chunks per single file in top-K.")
      .addSlider((s) =>
        s
          .setLimits(1, 5, 1)
          .setValue(this.plugin.settings.maxChunksPerFile)
          .setDynamicTooltip()
          .onChange(async (v) => {
            this.plugin.settings.maxChunksPerFile = v;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Enable reranker")
      .setDesc("Use bge-reranker-v2-m3 (slower, higher recall). Requires phase1d extra on the server.")
      .addToggle((t) =>
        t.setValue(this.plugin.settings.rerankerEnabled).onChange(async (v) => {
          this.plugin.settings.rerankerEnabled = v;
          await this.plugin.saveSettings();
        }),
      );

    new Setting(containerEl)
      .setName("Skip query analyzer")
      .setDesc("Bypass Haiku filter extraction; faster but no metadata WHERE clause.")
      .addToggle((t) =>
        t.setValue(this.plugin.settings.noAnalyze).onChange(async (v) => {
          this.plugin.settings.noAnalyze = v;
          await this.plugin.saveSettings();
        }),
      );

    new Setting(containerEl)
      .setName("Send active note as context hint")
      .setDesc("OFF (default): always search the entire vault. ON: prepend the open note's name to the query for 'this note' focus.")
      .addToggle((t) =>
        t.setValue(this.plugin.settings.sendActiveNote).onChange(async (v) => {
          this.plugin.settings.sendActiveNote = v;
          await this.plugin.saveSettings();
        }),
      );
  }
}
