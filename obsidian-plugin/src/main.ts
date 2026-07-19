import { Notice, Plugin, WorkspaceLeaf } from "obsidian";
import { KrunRagApi } from "./api";
import { CompanyPickerModal } from "./company-modal";
import { DEFAULT_SETTINGS, KrunRagSettings, KrunRagSettingTab } from "./settings";
import { KRUN_RAG_VIEW_TYPE, KrunRagView } from "./view";

export default class KrunRagPlugin extends Plugin {
  settings: KrunRagSettings = DEFAULT_SETTINGS;

  async onload(): Promise<void> {
    await this.loadSettings();

    this.registerView(KRUN_RAG_VIEW_TYPE, (leaf) => new KrunRagView(leaf, this));

    this.addRibbonIcon("messages-square", "Open KRUN RAG", () => this.activateView());

    this.addCommand({
      id: "open-view",
      name: "Open KRUN RAG sidebar",
      callback: () => this.activateView(),
    });

    this.addCommand({
      id: "ask-about-current-note",
      name: "Ask about current note",
      checkCallback: (checking) => {
        const f = this.app.workspace.getActiveFile();
        if (!f) return false;
        if (!checking) {
          this.activateView().then((view) => {
            if (view) {
              const stem = f.basename;
              view.focusAndPrefill(`[[${stem}]]에 대해 알려줘 — 핵심 요약과 다음에 챙길 점은?`);
            }
          });
        }
        return true;
      },
    });

    this.addCommand({
      id: "company-brief",
      name: "회사 브리핑 (미팅 전 맥락 복원)",
      callback: async () => {
        const view = await this.activateView();
        if (!view) return;
        const entries = await new KrunRagApi(this.settings.serverUrl).companies();
        if (!entries.length) {
          new Notice("회사 목록을 가져오지 못했습니다. RAG 서버가 실행 중인지 확인하세요.");
          return;
        }
        const modal = new CompanyPickerModal(this.app, entries, (c) => view.runCompanyBrief(c));
        modal.open();
        // Pre-filter to the company whose folder the user is currently in.
        const guess = this.guessCompanyFromActiveFile();
        if (guess) {
          modal.inputEl.value = guess;
          modal.inputEl.dispatchEvent(new Event("input"));
        }
      },
    });

    this.addCommand({
      id: "ask-quick",
      name: "Ask KRUN RAG…",
      callback: () => {
        this.activateView().then((view) => view?.focusAndPrefill(""));
      },
    });

    this.addCommand({
      id: "reindex-incremental",
      name: "Reindex vault (incremental)",
      callback: async () => {
        const view = await this.activateView();
        if (view) {
          new Notice("Reindex requested via sidebar Health/Reindex button.");
        }
      },
    });

    this.addSettingTab(new KrunRagSettingTab(this.app, this));
  }

  onunload(): void {
    // Obsidian unmounts views automatically; ItemView.onClose aborts in-flight streams.
  }

  /** Company folder name if the active file lives under 03_Companies/, else null.
   *
   * Vault layout is 03_Companies/{Antonio|KRUN}/{단계}/{회사}/{파일}.md, so the
   * containing folder is the company for both the profile note and its meetings.
   */
  private guessCompanyFromActiveFile(): string | null {
    const f = this.app.workspace.getActiveFile();
    if (!f) return null;
    const parts = f.path.split("/");
    if (parts[0] !== "03_Companies" || parts.length < 3) return null;
    return parts[parts.length - 2] || null;
  }

  async loadSettings(): Promise<void> {
    this.settings = { ...DEFAULT_SETTINGS, ...(await this.loadData()) };
  }

  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
    // Push base URL change to any open view.
    for (const leaf of this.app.workspace.getLeavesOfType(KRUN_RAG_VIEW_TYPE)) {
      const v = leaf.view;
      if (v instanceof KrunRagView) v.rebindApi();
    }
  }

  async activateView(): Promise<KrunRagView | null> {
    const { workspace } = this.app;
    let leaf: WorkspaceLeaf | null = workspace.getLeavesOfType(KRUN_RAG_VIEW_TYPE)[0] ?? null;
    if (!leaf) {
      leaf = workspace.getRightLeaf(false);
      if (!leaf) return null;
      await leaf.setViewState({ type: KRUN_RAG_VIEW_TYPE, active: true });
    }
    workspace.revealLeaf(leaf);
    return leaf.view instanceof KrunRagView ? leaf.view : null;
  }
}
