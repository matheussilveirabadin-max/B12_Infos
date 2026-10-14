"""
AvisoSonoro — toca avisos sonoros em intervalos fixos durante o dia.

Ciclo (padrão a cada 15 min, no horário configurado):
  1. Fade-out de TODOS os apps que tocam som (Chrome, Edge, Firefox, Spotify,
     Teams, etc.) até 15% em 4 s — controle por sessão de áudio do Windows,
     em todos os dispositivos de saída ativos.
  2. Fade-in do player próprio até 50% em 2 s.
  3. Toca o(s) áudio(s), uma única vez cada:
       modo "rodizio"   -> UM áudio por aviso, alternando 1, 2, 3, 1, 2, 3...
       modo "sequencia" -> todos os áudios em sequência a cada aviso.
  4. Fade-in dos apps de volta ao volume exato que cada um tinha.

Requisitos: Windows 10/11. Dependências: pycaw, comtypes, psutil, pygame, pystray, Pillow.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

APP_NAME = "AvisoSonoro"
EXTENSOES = {".mp3", ".wav", ".ogg", ".flac", ".opus"}
SESSAO_EXPIRADA = 2  # AudioSessionStateExpired

DEFAULT_CONFIG = {
    "modo": "rodizio",
    "rodizio_reinicia_cada_dia": False,
    "audios": [],
    "pasta_audios": "audios",
    "quantidade_audios": 3,
    "intervalo_minutos": 15,
    "horario_inicio": "08:00",
    "horario_fim": "18:00",
    "dias_semana": [0, 1, 2, 3, 4],
    "volume_apps_durante_aviso": 0.15,
    "fade_saida_apps_seg": 4.0,
    "volume_player": 0.50,
    "fade_entrada_player_seg": 2.0,
    "fade_retorno_apps_seg": 3.0,
    "pausa_entre_audios_seg": 0.3,
    "tolerancia_atraso_seg": 120,
    "iniciar_com_windows": True,
}

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------- #
# Caminhos, log e configuração
# --------------------------------------------------------------------------- #
def pasta_base() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def pasta_gravavel() -> Path:
    """Pasta do .exe se for gravável; senão %LOCALAPPDATA%\\AvisoSonoro."""
    base = pasta_base()
    try:
        teste = base / ".teste_escrita"
        teste.write_text("ok")
        teste.unlink()
        return base
    except OSError:
        alt = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
        alt.mkdir(parents=True, exist_ok=True)
        return alt


def configurar_log() -> Path:
    pasta = pasta_gravavel() / "logs"
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / "aviso_sonoro.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(arquivo, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stderr is not None:  # no .exe sem console, stderr é None
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)
    return arquivo


def _hm(texto: str) -> dt.time:
    h, m = texto.strip().split(":")
    return dt.time(int(h), int(m))


def carregar_config(anterior: dict | None = None) -> dict:
    caminho = pasta_base() / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if not caminho.exists():
        try:
            caminho.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return cfg
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8-sig"))
        cfg.update({k: v for k, v in dados.items() if not k.startswith("_")})
        _hm(cfg["horario_inicio"]), _hm(cfg["horario_fim"])
        if int(cfg["intervalo_minutos"]) < 1:
            raise ValueError("intervalo_minutos deve ser >= 1")
        return cfg
    except Exception as exc:  # config inválida: mantém a última válida
        log.error("config.json inválido (%s). Usando configuração anterior/padrão.", exc)
        return anterior or dict(DEFAULT_CONFIG)


def _versao_config() -> tuple | None:
    """Data de modificação + tamanho do config.json (muda quando o arquivo é salvo)."""
    try:
        st = (pasta_base() / "config.json").stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def modo_rodizio(cfg: dict) -> bool:
    modo = str(cfg.get("modo", "rodizio")).strip().lower()
    if modo not in ("rodizio", "sequencia"):
        log.warning('modo "%s" desconhecido; usando "rodizio".', modo)
    return modo != "sequencia"


def resolver_audios(cfg: dict) -> list[Path]:
    base = pasta_base()
    if cfg["audios"]:
        candidatos = [(base / a) if not Path(a).is_absolute() else Path(a) for a in cfg["audios"]]
    else:
        pasta = base / cfg["pasta_audios"]
        candidatos = sorted(
            (p for p in pasta.glob("*") if p.suffix.lower() in EXTENSOES),
            key=lambda p: p.name.lower(),
        )
        if not modo_rodizio(cfg):  # no rodízio entram todos os áudios da pasta
            candidatos = candidatos[: int(cfg["quantidade_audios"])]
    validos = []
    for p in candidatos:
        if not p.is_file():
            log.warning("Áudio não encontrado: %s", p)
        elif p.suffix.lower() not in EXTENSOES:
            log.warning("Formato não suportado (use MP3/WAV/OGG/FLAC): %s", p.name)
        else:
            validos.append(p)
    return validos


class Rodizio:
    """Lembra o último áudio tocado (em disco) para alternar 1, 2, 3, 1... mesmo após reiniciar."""

    def __init__(self, arquivo: Path):
        self.arquivo = arquivo

    def _ler(self) -> dict:
        try:
            return json.loads(self.arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def proximo(self, arquivos: list[Path], reinicia_cada_dia: bool = False) -> Path:
        est = self._ler()
        ultimo = str(est.get("ultimo") or "").lower()
        if not ultimo or (reinicia_cada_dia and est.get("data") != dt.date.today().isoformat()):
            return arquivos[0]
        nomes = [p.name.lower() for p in arquivos]
        if ultimo in nomes:
            return arquivos[(nomes.index(ultimo) + 1) % len(arquivos)]
        for p in arquivos:  # último foi removido/renomeado: segue a ordem alfabética
            if p.name.lower() > ultimo:
                return p
        return arquivos[0]

    def registrar(self, arquivo: Path) -> None:
        try:
            self.arquivo.write_text(
                json.dumps({"ultimo": arquivo.name, "data": dt.date.today().isoformat()}),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Não foi possível salvar a posição do rodízio: %s", exc)


# --------------------------------------------------------------------------- #
# Agenda
# --------------------------------------------------------------------------- #
def proximo_horario(agora: dt.datetime, cfg: dict, ultimo: dt.datetime | None = None) -> dt.datetime | None:
    """Próximo slot = horario_inicio + k*intervalo, dentro de [inicio, fim], nos dias configurados."""
    ini, fim = _hm(cfg["horario_inicio"]), _hm(cfg["horario_fim"])
    passo = dt.timedelta(minutes=int(cfg["intervalo_minutos"]))
    dias = {int(d) for d in cfg["dias_semana"]}
    agora = agora.replace(microsecond=0)
    for d in range(8):
        dia = agora.date() + dt.timedelta(days=d)
        if dia.weekday() not in dias:
            continue
        primeiro = dt.datetime.combine(dia, ini)
        limite = dt.datetime.combine(dia, fim)
        cand = primeiro
        if agora > primeiro:
            k = -((primeiro - agora) // passo)  # teto da divisão
            cand = primeiro + k * passo
        if ultimo is not None and cand <= ultimo:
            cand = ultimo + passo
        if cand <= limite:
            return cand
    return None


def motivo_sem_horario(cfg: dict) -> str:
    dias = [int(d) for d in cfg.get("dias_semana", [])]
    if not dias:
        return '"dias_semana" está vazio no config.json (use, por exemplo, [0, 1, 2, 3, 4]).'
    if any(d < 0 or d > 6 for d in dias):
        return '"dias_semana" só aceita números de 0 (segunda) a 6 (domingo).'
    if _hm(cfg["horario_fim"]) < _hm(cfg["horario_inicio"]):
        return (f'"horario_fim" ({cfg["horario_fim"]}) é anterior a '
                f'"horario_inicio" ({cfg["horario_inicio"]}) no config.json.')
    return "verifique horario_inicio, horario_fim e dias_semana no config.json."


# --------------------------------------------------------------------------- #
# Volume por aplicativo (Windows Core Audio via pycaw)
# --------------------------------------------------------------------------- #
class ControleVolumeApps:
    """Abaixa/restaura o volume de cada sessão de áudio (por app), exceto a nossa."""

    def __init__(self, arquivo_estado: Path):
        self.pids_proprios = {os.getpid(), os.getppid()}  # .exe onefile = 2 processos
        self.arquivo_estado = arquivo_estado
        self.sessoes: dict[tuple, dict] = {}

    # -- enumeração -------------------------------------------------------- #
    def _listar(self):
        import comtypes
        from pycaw.api.audiopolicy import IAudioSessionControl2, IAudioSessionManager2
        from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
        from pycaw.constants import CLSID_MMDeviceEnumerator, DEVICE_STATE, EDataFlow
        from pycaw.utils import AudioSession, AudioUtilities

        try:
            enum = comtypes.CoCreateInstance(
                CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER
            )
            col = enum.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
            for i in range(col.GetCount()):
                dev = col.Item(i)
                try:
                    dev_id = dev.GetId()
                    mgr = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
                    mgr = mgr.QueryInterface(IAudioSessionManager2)
                    se = mgr.GetSessionEnumerator()
                    for j in range(se.GetCount()):
                        ctl = se.GetSession(j)
                        if ctl is not None:
                            yield dev_id, AudioSession(ctl.QueryInterface(IAudioSessionControl2))
                except Exception as exc:
                    log.debug("Dispositivo ignorado: %s", exc)
        except Exception as exc:  # fallback: só o dispositivo padrão
            log.warning("Enumeração de dispositivos falhou (%s); usando dispositivo padrão.", exc)
            for s in AudioUtilities.GetAllSessions():
                yield "padrao", s

    @staticmethod
    def _nome(sessao) -> str:
        try:
            proc = sessao.Process
            return proc.name() if proc else "Sons do sistema"
        except Exception:
            return f"pid {sessao.ProcessId}"

    # -- operações --------------------------------------------------------- #
    def abaixar(self, alvo: float, duracao: float) -> None:
        """Abaixa sessões ainda não controladas até `alvo` com rampa linear."""
        novas = []
        for dev_id, s in self._listar():
            try:
                if s.ProcessId in self.pids_proprios or s.State == SESSAO_EXPIRADA:
                    continue
                chave = (dev_id, s.InstanceIdentifier)
                if chave in self.sessoes:
                    continue
                vol = s.SimpleAudioVolume
                original = float(vol.GetMasterVolume())
                if original <= alvo + 0.005:
                    continue  # já está baixo: não mexe
                info = {"vol": vol, "original": original, "definido": original, "nome": self._nome(s)}
                self.sessoes[chave] = info
                novas.append(info)
            except Exception as exc:
                log.debug("Sessão ignorada: %s", exc)
        if not novas:
            return
        log.info("Abaixando: %s", ", ".join(f"{i['nome']} ({i['original']:.0%})" for i in novas))
        self._salvar_estado(alvo)
        self._rampa(novas, lambda i: i["original"], lambda i: alvo, duracao)

    def restaurar(self, duracao: float) -> None:
        if not self.sessoes:
            self._limpar_estado()
            return
        voltar = []
        for info in self.sessoes.values():
            try:
                atual = float(info["vol"].GetMasterVolume())
                if abs(atual - info["definido"]) > 0.02:
                    log.info("%s: volume alterado pelo usuário durante o aviso; mantido.", info["nome"])
                    continue
                voltar.append(info)
            except Exception:
                pass  # sessão encerrada
        self._rampa(voltar, lambda i: i["definido"], lambda i: i["original"], duracao)
        if voltar:
            log.info("Volume restaurado: %s", ", ".join(f"{i['nome']} ({i['original']:.0%})" for i in voltar))
        self.sessoes.clear()
        self._limpar_estado()

    @staticmethod
    def _rampa(itens, de, para, duracao: float) -> None:
        if not itens:
            return
        passos = max(1, int(duracao / 0.04))
        intervalo = duracao / passos
        for n in range(1, passos + 1):
            frac = n / passos
            for i in list(itens):
                v = min(1.0, max(0.0, de(i) + (para(i) - de(i)) * frac))
                try:
                    i["vol"].SetMasterVolume(v, None)
                    i["definido"] = v
                except Exception:
                    itens.remove(i)
            time.sleep(intervalo)

    # -- proteção contra queda (luz, kill do processo) ---------------------- #
    def _salvar_estado(self, alvo: float) -> None:
        dados = {"alvo": alvo, "apps": {}}
        for info in self.sessoes.values():
            dados["apps"].setdefault(info["nome"].lower(), info["original"])
        try:
            self.arquivo_estado.write_text(json.dumps(dados), encoding="utf-8")
        except OSError:
            pass

    def _limpar_estado(self) -> None:
        try:
            self.arquivo_estado.unlink()
        except OSError:
            pass

    def recuperar_queda(self) -> None:
        """Se o programa caiu no meio de um aviso, devolve o volume dos apps."""
        if not self.arquivo_estado.exists():
            return
        try:
            dados = json.loads(self.arquivo_estado.read_text(encoding="utf-8"))
            alvo, apps = float(dados["alvo"]), dados["apps"]
            for _, s in self._listar():
                nome = self._nome(s).lower()
                if nome in apps:
                    vol = s.SimpleAudioVolume
                    if abs(float(vol.GetMasterVolume()) - alvo) < 0.02:
                        vol.SetMasterVolume(float(apps[nome]), None)
                        log.info("Recuperação: %s voltou para %.0f%%", nome, apps[nome] * 100)
        except Exception as exc:
            log.warning("Falha ao recuperar estado anterior: %s", exc)
        self._limpar_estado()

    def listar_texto(self) -> list[str]:
        linhas = []
        for dev_id, s in self._listar():
            try:
                linhas.append(f"{self._nome(s):30s} pid={s.ProcessId:<6} vol={s.SimpleAudioVolume.GetMasterVolume():.0%}")
            except Exception:
                pass
        return linhas


# --------------------------------------------------------------------------- #
# Player
# --------------------------------------------------------------------------- #
class Player:
    def __init__(self, parar: threading.Event):
        self.parar = parar

    def tocar_sequencia(self, arquivos, volume: float, fade_in: float, pausa: float, tick) -> int:
        """Toca cada arquivo uma vez, em ordem. Retorna quantos foram tocados."""
        import pygame

        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        musica = pygame.mixer.music
        tocados = 0
        try:
            for arq in arquivos:
                if self.parar.is_set():
                    break
                try:
                    musica.load(str(arq))
                except pygame.error as exc:
                    log.error("Não foi possível abrir %s: %s", arq.name, exc)
                    continue
                primeiro = tocados == 0
                musica.set_volume(0.0 if primeiro else volume)
                musica.play(loops=0)  # loops=0 -> uma única vez
                log.info("Tocando %s", arq.name)
                inicio = time.monotonic()
                if primeiro and fade_in > 0:
                    while ((t := time.monotonic() - inicio) < fade_in and musica.get_busy()
                           and not self.parar.is_set()):
                        musica.set_volume(volume * t / fade_in)
                        time.sleep(0.03)
                    musica.set_volume(volume)
                while musica.get_busy() and not self.parar.is_set():
                    tick()
                    time.sleep(0.1)
                    if time.monotonic() - inicio > 3600:  # trava de segurança
                        log.warning("%s passou de 1 h; interrompido.", arq.name)
                        break
                musica.stop()
                musica.unload()
                tocados += 1
                if pausa > 0 and arq is not arquivos[-1]:
                    time.sleep(pausa)
        finally:
            pygame.mixer.quit()
        return tocados


# --------------------------------------------------------------------------- #
# Aplicação
# --------------------------------------------------------------------------- #
class App:
    def __init__(self):
        self.cfg = carregar_config()
        self.parar = threading.Event()
        self.sinal = threading.Event()  # acorda o laço (sair / tocar agora)
        self.tocar_agora = False
        self.pausado = False
        self.proximo: dt.datetime | None = None
        self.em_ciclo = threading.Lock()
        self.icone = None
        self.primeira_execucao = False
        self.arquivo_estado = pasta_gravavel() / "estado_volume.json"
        self.rodizio = Rodizio(pasta_gravavel() / "estado_rodizio.json")

    # -- ciclo de aviso ---------------------------------------------------- #
    def executar_ciclo(self, motivo: str = "agendado") -> None:
        if not self.em_ciclo.acquire(blocking=False):
            log.info("Ciclo já em andamento; ignorado.")
            return
        try:
            self.cfg = carregar_config(self.cfg)  # sempre usa o config.json mais recente
            cfg = self.cfg
            arquivos = resolver_audios(cfg)
            if not arquivos:
                pasta = pasta_base() / cfg["pasta_audios"]
                outros = sorted(p.name for p in pasta.glob("*")
                                if p.is_file() and p.suffix.lower() not in EXTENSOES
                                and not p.name.upper().startswith("COLOQUE"))
                if not pasta.is_dir():
                    msg = f"A pasta '{cfg['pasta_audios']}' não existe ao lado do AvisoSonoro.exe."
                elif outros:
                    msg = (f"Formato não suportado: {', '.join(outros[:3])}. "
                           "Converta para MP3.")
                else:
                    msg = f"A pasta '{cfg['pasta_audios']}' está vazia. Coloque os áudios nela."
                log.warning("Nenhum áudio válido em %s. %s", pasta, msg)
                self.avisar("AvisoSonoro: nenhum áudio tocado", msg)
                return
            if modo_rodizio(cfg):
                escolhido = self.rodizio.proximo(arquivos, bool(cfg.get("rodizio_reinicia_cada_dia")))
                pos = arquivos.index(escolhido) + 1
                arquivos = [escolhido]
                # avança já aqui: um arquivo com defeito não trava o rodízio no mesmo áudio
                self.rodizio.registrar(escolhido)
                log.info("Início do aviso (%s): rodízio, áudio %d de %d — %s",
                         motivo, pos, len(resolver_audios(cfg)), escolhido.name)
            else:
                log.info("Início do aviso (%s): sequência de %d áudio(s)", motivo, len(arquivos))
            ctl = ControleVolumeApps(self.arquivo_estado)
            alvo = float(cfg["volume_apps_durante_aviso"])
            ultimo_tick = [0.0]

            def tick():  # apps que começam a tocar durante o aviso também são abaixados
                if time.monotonic() - ultimo_tick[0] >= 1.0:
                    ultimo_tick[0] = time.monotonic()
                    ctl.abaixar(alvo, 0.4)

            try:
                ctl.abaixar(alvo, float(cfg["fade_saida_apps_seg"]))
                n = Player(self.parar).tocar_sequencia(
                    arquivos,
                    float(cfg["volume_player"]),
                    float(cfg["fade_entrada_player_seg"]),
                    float(cfg["pausa_entre_audios_seg"]),
                    tick,
                )
                log.info("Fim do aviso: %d áudio(s) tocado(s).", n)
            finally:
                ctl.restaurar(float(cfg["fade_retorno_apps_seg"]))
        except Exception as exc:
            log.exception("Erro no ciclo de aviso")
            self.avisar("AvisoSonoro: erro ao tocar", f"{exc} (detalhes em logs\\aviso_sonoro.log)")
        finally:
            self.em_ciclo.release()

    def avisar(self, titulo: str, mensagem: str) -> None:
        """Balão de notificação do Windows (se o ícone estiver ativo)."""
        if self.icone is None:
            return
        try:
            self.icone.notify(mensagem[:250], titulo[:60])
        except Exception:
            pass

    # -- laço do agendador ------------------------------------------------- #
    def laco(self) -> None:
        com = _com_init()
        try:
            ControleVolumeApps(self.arquivo_estado).recuperar_queda()
            ultimo = None
            while not self.parar.is_set():
                self.cfg = carregar_config(self.cfg)
                versao_cfg = _versao_config()
                alvo = proximo_horario(dt.datetime.now(), self.cfg, ultimo)
                self.proximo = alvo
                self._atualizar_titulo()
                if alvo is None:
                    motivo = motivo_sem_horario(self.cfg)
                    log.warning("Nenhum aviso será agendado: %s", motivo)
                    self.avisar("AvisoSonoro: nenhum horário agendado", motivo)
                else:
                    log.info("Próximo aviso: %s", alvo.strftime("%d/%m %H:%M"))
                config_mudou = False
                while not self.parar.is_set() and not self.tocar_agora:
                    falta = 3600.0 if alvo is None else (alvo - dt.datetime.now()).total_seconds()
                    if falta <= 0:
                        break
                    # 5 s: detecta edição do config.json e tolera suspensão/ajuste de relógio
                    self.sinal.wait(min(falta, 5))
                    self.sinal.clear()
                    if _versao_config() != versao_cfg:
                        log.info("config.json alterado; aplicando nova configuração.")
                        config_mudou = True
                        break
                if config_mudou:
                    continue
                if alvo is None and not self.tocar_agora:
                    continue
                if self.parar.is_set():
                    break
                if self.tocar_agora:
                    self.tocar_agora = False
                    self.executar_ciclo("manual")
                    continue
                ultimo = alvo
                atraso = (dt.datetime.now() - alvo).total_seconds()
                if atraso > float(self.cfg["tolerancia_atraso_seg"]):
                    log.info("Horário %s perdido (PC suspenso?); pulando.", alvo.strftime("%H:%M"))
                elif self.pausado:
                    log.info("Avisos pausados; %s ignorado.", alvo.strftime("%H:%M"))
                else:
                    self.executar_ciclo()
        finally:
            _com_uninit(com)

    # -- controles --------------------------------------------------------- #
    def solicitar_tocar(self) -> None:
        self.tocar_agora = True
        self.sinal.set()

    def encerrar(self) -> None:
        self.parar.set()
        self.sinal.set()

    def _atualizar_titulo(self) -> None:
        if self.icone is None:
            return
        txt = "pausado" if self.pausado else (
            f"próximo: {self.proximo:%d/%m %H:%M}" if self.proximo else "sem horário")
        try:
            if not self.pausado and modo_rodizio(self.cfg):
                arquivos = resolver_audios(self.cfg)
                if arquivos:
                    prox = self.rodizio.proximo(arquivos, bool(self.cfg.get("rodizio_reinicia_cada_dia")))
                    txt += f" ({prox.stem})"
        except Exception:
            pass
        self.icone.title = f"{APP_NAME} — {txt}"[:120]  # limite do Windows para a dica do ícone


def _com_init():
    try:
        import comtypes
        comtypes.CoInitialize()
        return comtypes
    except Exception:
        return None


def _com_uninit(com) -> None:
    if com is not None:
        try:
            com.CoUninitialize()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Windows: instância única e inicialização automática
# --------------------------------------------------------------------------- #
_mutex = None


def instancia_unica() -> bool:
    global _mutex
    if os.name != "nt":
        return True
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = k32.CreateMutexW(None, False, f"Local\\{APP_NAME}_mutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _comando_inicio() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else sys.executable}" "{Path(__file__).resolve()}"'


def inicio_automatico(ativar: bool | None = None) -> bool:
    """Consulta (ativar=None) ou liga/desliga a inicialização com o Windows (HKCU, sem admin)."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CHAVE_RUN, 0,
                        winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
        if ativar is True:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _comando_inicio())
        elif ativar is False:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass
        try:
            winreg.QueryValueEx(k, APP_NAME)
            return True
        except FileNotFoundError:
            return False


def preparar_primeira_execucao(cfg: dict) -> bool:
    """1ª execução: liga o início automático sozinho (zero configuração para o usuário).
    Execuções seguintes: só atualiza o caminho caso a pasta tenha sido movida."""
    if os.name != "nt":
        return False
    marcador = pasta_gravavel() / ".instalado"
    primeira = not marcador.exists()
    try:
        if primeira and cfg.get("iniciar_com_windows", True):
            inicio_automatico(True)
            log.info("Primeira execução: início automático com o Windows ativado.")
        elif inicio_automatico():
            inicio_automatico(True)  # regrava o caminho atual do .exe
        marcador.write_text(dt.datetime.now().isoformat(), encoding="utf-8")
    except Exception as exc:
        log.warning("Não foi possível configurar o início automático: %s", exc)
    return primeira


# --------------------------------------------------------------------------- #
# Bandeja do sistema
# --------------------------------------------------------------------------- #
def _imagem_icone():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(22, 101, 192))
    d.polygon([(14, 26), (24, 26), (36, 16), (36, 48), (24, 38), (14, 38)], fill="white")
    d.arc((30, 20, 50, 44), -50, 50, fill="white", width=4)
    return img


def rodar_bandeja(app: App) -> bool:
    try:
        import pystray
    except Exception as exc:
        log.warning("Bandeja indisponível (%s); rodando sem ícone.", exc)
        return False

    def alternar_pausa(icon, item):
        app.pausado = not app.pausado
        log.info("Avisos %s.", "pausados" if app.pausado else "retomados")
        app._atualizar_titulo()

    def alternar_inicio(icon, item):
        try:
            inicio_automatico(not inicio_automatico())
        except Exception as exc:
            log.error("Não foi possível alterar a inicialização automática: %s", exc)

    def abrir_pasta(icon, item):
        os.startfile(pasta_base())  # type: ignore[attr-defined]

    def sair(icon, item):
        app.encerrar()
        icon.stop()

    def esta_no_inicio(item):
        try:
            return inicio_automatico()
        except Exception:
            return False

    menu = pystray.Menu(
        pystray.MenuItem("Tocar agora", lambda i, it: app.solicitar_tocar(), default=True),
        pystray.MenuItem("Pausar avisos", alternar_pausa, checked=lambda it: app.pausado),
        pystray.MenuItem("Iniciar com o Windows", alternar_inicio, checked=esta_no_inicio),
        pystray.MenuItem("Abrir pasta (áudios, config, logs)", abrir_pasta),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Sair", sair),
    )
    def ao_abrir(icon):
        icon.visible = True
        if app.primeira_execucao:
            try:
                icon.notify("Os avisos já estão ativos e vão ligar sozinhos com o computador.",
                            "AvisoSonoro instalado")
            except Exception:
                pass

    app.icone = pystray.Icon(APP_NAME, _imagem_icone(), APP_NAME, menu)
    app._atualizar_titulo()
    app.icone.run(setup=ao_abrir)  # bloqueia até "Sair"
    return True


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(prog=APP_NAME)
    ap.add_argument("--testar", action="store_true", help="executa um ciclo agora e sai")
    ap.add_argument("--sem-bandeja", action="store_true", help="roda sem ícone na bandeja")
    ap.add_argument("--listar-sessoes", action="store_true", help="registra no log os apps com áudio")
    ap.add_argument("--instalar-inicializacao", action="store_true")
    ap.add_argument("--remover-inicializacao", action="store_true")
    args, _ = ap.parse_known_args()

    arquivo_log = configurar_log()
    log.info("=== %s iniciado (log: %s) ===", APP_NAME, arquivo_log)

    if args.instalar_inicializacao or args.remover_inicializacao:
        ok = inicio_automatico(bool(args.instalar_inicializacao))
        log.info("Inicialização automática: %s", "ativa" if ok else "desativada")
        return 0

    if args.listar_sessoes:
        com = _com_init()
        for linha in ControleVolumeApps(pasta_gravavel() / "estado_volume.json").listar_texto():
            log.info("  %s", linha)
        _com_uninit(com)
        return 0

    app = App()
    app.primeira_execucao = preparar_primeira_execucao(app.cfg)

    if args.testar:
        com = _com_init()
        try:
            app.executar_ciclo("teste")
        finally:
            _com_uninit(com)
        return 0

    if not instancia_unica():
        log.info("Já existe uma instância em execução; saindo.")
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "O AvisoSonoro já está aberto (ícone azul perto do relógio).\n\n"
                "Para trocar de versão ou de pasta: clique com o botão direito "
                "no ícone azul > Sair, e abra este AvisoSonoro.exe de novo.",
                APP_NAME, 0x40)  # MB_ICONINFORMATION
        except Exception:
            pass
        return 0

    trabalhador = threading.Thread(target=app.laco, name="agendador", daemon=True)
    trabalhador.start()
    if args.sem_bandeja or not rodar_bandeja(app):
        try:
            while trabalhador.is_alive():
                trabalhador.join(1)
        except KeyboardInterrupt:
            app.encerrar()
    app.encerrar()
    trabalhador.join(timeout=15)  # deixa restaurar o volume se estava no meio de um aviso
    log.info("=== %s encerrado ===", APP_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
