"""Janela da Máquina de Cortes."""
import json
import os
import queue
import tempfile
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import motor

CONFIG = Path(__file__).resolve().parent / "config.json"
REGISTRO = Path(__file__).resolve().parent / "registro.txt"


def carregar_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Máquina de Cortes")
        self.geometry("820x720")
        self.minsize(640, 560)
        self.fila: queue.Queue = queue.Queue()
        self.pasta_saida: Path | None = None
        cfg = carregar_config()

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Máquina de Cortes", font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # Fonte
        ttk.Label(frm, text="Vídeo ou link:").grid(row=1, column=0, sticky="w", **pad)
        self.fonte = tk.StringVar()
        ttk.Entry(frm, textvariable=self.fonte).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(frm, text="Escolher arquivo...", command=self.escolher).grid(row=1, column=2, **pad)

        # Chave
        ttk.Label(frm, text="Chave da API (opcional):").grid(row=2, column=0, sticky="w", **pad)
        self.chave = tk.StringVar(value=cfg.get("api_key", ""))
        ttk.Entry(frm, textvariable=self.chave, show="•").grid(row=2, column=1, columnspan=2, sticky="ew", **pad)

        # Números
        nums = ttk.Frame(frm)
        nums.grid(row=3, column=0, columnspan=3, sticky="w", **pad)
        self.qtd = tk.IntVar(value=cfg.get("qtd_cortes", 5))
        self.dmin = tk.IntVar(value=cfg.get("dur_min", 30))
        self.dmax = tk.IntVar(value=cfg.get("dur_max", 75))
        self.whisper = tk.StringVar(value=cfg.get("modelo_whisper", "small"))
        ttk.Label(nums, text="Cortes:").pack(side="left")
        ttk.Spinbox(nums, from_=1, to=30, width=4, textvariable=self.qtd).pack(side="left", padx=(4, 14))
        ttk.Label(nums, text="Duração (s):").pack(side="left")
        ttk.Spinbox(nums, from_=10, to=180, width=4, textvariable=self.dmin).pack(side="left", padx=4)
        ttk.Label(nums, text="a").pack(side="left")
        ttk.Spinbox(nums, from_=15, to=600, width=4, textvariable=self.dmax).pack(side="left", padx=(4, 14))
        ttk.Label(nums, text="Precisão da transcrição:").pack(side="left")
        ttk.Combobox(nums, textvariable=self.whisper, width=9, state="readonly",
                     values=["tiny", "base", "small", "medium", "large-v3"]).pack(side="left", padx=4)
        self.bloco = tk.IntVar(value=cfg.get("bloco_min", 30))
        ttk.Label(nums, text="   Live: cortar a cada").pack(side="left")
        ttk.Spinbox(nums, from_=5, to=120, width=4, textvariable=self.bloco).pack(side="left", padx=4)
        ttk.Label(nums, text="min").pack(side="left")

        # Edição
        ed = ttk.LabelFrame(frm, text="Edição", padding=8)
        ed.grid(row=4, column=0, columnspan=3, sticky="ew", **pad)
        self.vertical = tk.BooleanVar(value=cfg.get("vertical", True))
        self.legendas = tk.BooleanVar(value=cfg.get("legendas", True))
        self.zoom = tk.BooleanVar(value=cfg.get("zoom", True))
        self.titulo = tk.BooleanVar(value=cfg.get("titulo", True))
        for txt, var in [("Vertical 9:16", self.vertical), ("Legendas automáticas", self.legendas),
                         ("Zoom lento", self.zoom), ("Título", self.titulo)]:
            ttk.Checkbutton(ed, text=txt, variable=var).pack(side="left", padx=8)

        lay = ttk.Frame(frm)
        lay.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(lay, text="Layout:").pack(side="left")
        self.layouts = {"Streamer (webcam em cima, jogo embaixo)": "streamer", "Normal (um quadro só)": "normal"}
        nome_atual = next((k for k, v in self.layouts.items() if v == cfg.get("layout", "streamer")),
                          next(iter(self.layouts)))
        self.layout = tk.StringVar(value=nome_atual)
        ttk.Combobox(lay, textvariable=self.layout, width=36, state="readonly",
                     values=list(self.layouts)).pack(side="left", padx=6)
        self.cam_regiao = cfg.get("cam_regiao")
        ttk.Button(lay, text="Marcar webcam...", command=self.marcar_webcam).pack(side="left", padx=6)
        ttk.Button(lay, text="Automática", command=self.webcam_auto).pack(side="left")
        self.cam_txt = tk.StringVar()
        ttk.Label(lay, textvariable=self.cam_txt, foreground="#666").pack(side="left", padx=8)
        self.atualizar_cam_txt()

        ttk.Label(frm, text="Instruções para a IA (opcional):").grid(row=6, column=0, columnspan=3, sticky="w", **pad)
        self.extras = tk.StringVar(value=cfg.get("instrucoes_extras", ""))
        ttk.Entry(frm, textvariable=self.extras).grid(row=7, column=0, columnspan=3, sticky="ew", **pad)

        # Botões
        bts = ttk.Frame(frm)
        bts.grid(row=8, column=0, columnspan=3, sticky="ew", pady=8)
        self.bt_ir = ttk.Button(bts, text="▶  Fazer cortes", command=self.iniciar)
        self.bt_ir.pack(side="left", padx=10)
        self.bt_parar = ttk.Button(bts, text="■  Parar live", command=self.parar_live, state="disabled")
        self.bt_parar.pack(side="left", padx=(0, 10))
        self.bt_pasta = ttk.Button(bts, text="Abrir pasta dos cortes", command=self.abrir_pasta)
        self.bt_pasta.pack(side="left")
        self.parar = threading.Event()
        self.barra = ttk.Progressbar(bts, mode="indeterminate", length=200)
        self.barra.pack(side="right", padx=10)

        # Log
        self.log_txt = tk.Text(frm, height=14, wrap="word", font=("Consolas", 9),
                               bg="#111", fg="#ddd", insertbackground="#ddd")
        self.log_txt.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=10, pady=4)
        frm.rowconfigure(9, weight=1)

        self.after(100, self.ler_fila)
        self.log("Cole um link (YouTube, Twitch...) ou escolha um vídeo do PC e clique em Fazer cortes.")

    # ------------------------------------------------------------------
    def escolher(self):
        p = filedialog.askopenfilename(filetypes=[("Vídeos", "*.mp4 *.mkv *.mov *.avi *.webm"), ("Todos", "*.*")])
        if p:
            self.fonte.set(p)

    def log(self, msg: str):
        self.log_txt.insert("end", msg + "\n")
        self.log_txt.see("end")
        try:  # cópia do registro em arquivo, para conferir erros depois
            with open(REGISTRO, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        except OSError:
            pass

    def ler_fila(self):
        while not self.fila.empty():
            tipo, valor = self.fila.get()
            if tipo == "log":
                self.log(valor)
            elif tipo == "quadro":
                self.abrir_marcador(valor)
            elif tipo == "fim":
                self.barra.stop()
                self.bt_ir.config(state="normal")
                self.bt_parar.config(state="disabled")
                if isinstance(valor, Path):
                    self.pasta_saida = valor
                    if messagebox.askyesno("Pronto!", "Cortes prontos. Abrir a pasta?"):
                        self.abrir_pasta()
                else:
                    messagebox.showerror("Erro", str(valor))
        self.after(100, self.ler_fila)

    def opcoes(self) -> motor.Opcoes:
        return motor.Opcoes(
            qtd_cortes=self.qtd.get(), dur_min=self.dmin.get(), dur_max=self.dmax.get(),
            modelo_whisper=self.whisper.get(), vertical=self.vertical.get(),
            legendas=self.legendas.get(), zoom=self.zoom.get(), titulo=self.titulo.get(),
            api_key=self.chave.get(), instrucoes_extras=self.extras.get(), bloco_min=self.bloco.get(),
            layout=self.layouts.get(self.layout.get(), "streamer"), cam_regiao=self.cam_regiao,
        )

    # ---------------- webcam ----------------
    def atualizar_cam_txt(self):
        self.cam_txt.set("webcam: marcada à mão" if self.cam_regiao else "webcam: achada automaticamente")

    def salvar_cfg(self):
        CONFIG.write_text(json.dumps(vars(self.opcoes()), ensure_ascii=False, indent=2), encoding="utf-8")

    def webcam_auto(self):
        self.cam_regiao = None
        self.atualizar_cam_txt()
        self.salvar_cfg()
        self.log("Webcam: voltando para a detecção automática do rosto.")

    def marcar_webcam(self):
        fonte = self.fonte.get().strip()
        if not fonte:
            messagebox.showwarning("Falta o vídeo", "Cole o link da live (ou escolha um vídeo) para eu pegar uma imagem.")
            return
        self.log("Pegando uma imagem do vídeo para marcar a webcam...")
        destino = Path(tempfile.gettempdir()) / "maquina_cortes_quadro.png"

        def trabalho():
            try:
                self.fila.put(("quadro", motor.capturar_quadro(fonte, destino)))
            except Exception as e:
                self.fila.put(("log", f"Erro ao pegar a imagem: {e}"))

        threading.Thread(target=trabalho, daemon=True).start()

    def abrir_marcador(self, png: Path):
        janela = tk.Toplevel(self)
        janela.title("Arraste um retângulo em volta do rosto do streamer")
        img = tk.PhotoImage(file=str(png))
        cv = tk.Canvas(janela, width=img.width(), height=img.height(), cursor="crosshair", highlightthickness=0)
        cv.pack()
        cv.create_image(0, 0, image=img, anchor="nw")
        cv.imagem = img  # mantém a imagem viva
        estado = {"ini": None, "ret": None, "caixa": None}

        def apertar(e):
            estado["ini"] = (e.x, e.y)
            if estado["ret"]:
                cv.delete(estado["ret"])
            estado["ret"] = cv.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff2d2d", width=3)

        def arrastar(e):
            x0, y0 = estado["ini"]
            cv.coords(estado["ret"], x0, y0, e.x, e.y)

        def soltar(e):
            x0, y0 = estado["ini"]
            x1, y1 = sorted((x0, e.x)), sorted((y0, e.y))
            w, h = img.width(), img.height()
            if x1[1] - x1[0] > 10 and y1[1] - y1[0] > 10:
                estado["caixa"] = [max(0, x1[0]) / w, max(0, y1[0]) / h,
                                   (min(w, x1[1]) - max(0, x1[0])) / w, (min(h, y1[1]) - max(0, y1[0])) / h]

        cv.bind("<ButtonPress-1>", apertar)
        cv.bind("<B1-Motion>", arrastar)
        cv.bind("<ButtonRelease-1>", soltar)

        def salvar():
            if not estado["caixa"]:
                messagebox.showwarning("Marque a webcam", "Arraste um retângulo em volta do rosto.", parent=janela)
                return
            self.cam_regiao = [round(v, 4) for v in estado["caixa"]]
            self.atualizar_cam_txt()
            self.salvar_cfg()
            self.log("Webcam marcada! Vale para os próximos cortes (fica salvo).")
            janela.destroy()

        rodape = ttk.Frame(janela, padding=8)
        rodape.pack(fill="x")
        ttk.Label(rodape, text="Marque só o rosto, sem as bordas da webcam. O corte fica centralizado no que você marcar."
                  ).pack(side="left")
        ttk.Button(rodape, text="Salvar", command=salvar).pack(side="right")

    def parar_live(self):
        self.parar.set()
        self.bt_parar.config(state="disabled")
        self.log("Pedido de parada recebido — terminando o último bloco...")

    def iniciar(self):
        fonte = self.fonte.get().strip()
        if not fonte:
            messagebox.showwarning("Falta o vídeo", "Cole um link ou escolha um arquivo.")
            return
        op = self.opcoes()
        sem_chave = not op.api_key.strip() and not os.environ.get("ANTHROPIC_API_KEY")
        if sem_chave and not motor.achar_claude_code():
            messagebox.showwarning(
                "Falta a IA",
                "Coloque uma chave da API (console.anthropic.com)\nou instale o Claude Code e faça login "
                "com sua conta Pro (veja o README).")
            return
        self.salvar_cfg()

        self.bt_ir.config(state="disabled")
        self.parar.clear()
        if motor.eh_link(fonte):
            self.bt_parar.config(state="normal")
        self.barra.start(12)
        self.log("-" * 60)
        if sem_chave:
            self.log("Sem chave de API: usando sua assinatura Claude pelo Claude Code.")

        def trabalho():
            try:
                pasta = motor.processar(fonte, op, lambda m: self.fila.put(("log", m)), self.parar)
                self.fila.put(("fim", pasta))
            except Exception as e:
                self.fila.put(("log", traceback.format_exc()))
                self.fila.put(("fim", e))

        threading.Thread(target=trabalho, daemon=True).start()

    def abrir_pasta(self):
        pasta = self.pasta_saida or motor.PASTA_SAIDA
        pasta.mkdir(parents=True, exist_ok=True)
        os.startfile(pasta)


if __name__ == "__main__":
    App().mainloop()
