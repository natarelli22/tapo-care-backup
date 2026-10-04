"""OSD Visual Synchronization Module for Tapo Care Backup.

Reads the on-screen display (OSD) timestamp from the video's first frame
using normalized cross-correlation edge template matching.
Detects camera pre-roll and synchronizes the recorded file and thumbnail
names to the exact visual start time.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta
import io
import logging
from pathlib import Path
import re
import subprocess

import numpy as np
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

# Search windows for HH:MM:SS in the 1000x60 crop
WINDOWS = [
    ("H1", 475, 505),
    ("H2", 520, 550),
    ("M1", 610, 640),
    ("M2", 655, 685),
    ("S1", 740, 770),
    ("S2", 795, 825),
]

_TEMPLATES_CACHE: dict[int, tuple[np.ndarray, float]] | None = None

# Embedded 50x28 px Edge Templates (0..9)
_TEMPLATES_B64: dict[int, str] = {
  "0": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAADSUlEQVR4nFXU24uVZRQG8N/7ft+39+w9M3tmdMbxnKcwL5QRQjGKoAgqwwiE0pRAKrupi7qpCynwOjD6AyIigqCb0uhERSkRRBkUQVEWEnloMjNHx5m9VxffjNq6XA9rPWut53lfUcfRENeFj+LokSANLbPkha3iGy2k0vhYBAeMK5OWZ4jwkKwAmyOcfHJYUST3LBEnh6/1/ZHAHlXZ2rdMxPDEa2Euws6I8KaiueB58bqOsPdQXL7wd4hgedityAveEbk5lkIMobHjTtEZHD8VcjWwu8Ge6cng3Hl9irc7fHthZglUb4momX68UVNz/OMJIZlLxidSFr7cYHFujDopcDYy+HrxeBu/Tuj2rnQGB8G4Gpw9dWrVMT64ZIbJCz+hyD2lAU58vmZh+5bk6L+6SNPoorRjr9VX/vplB5Fy2c09xTkUuhSHRXja9hAoGGocFlIWOW1nlePOg1yUypyQepQJv/nBZdC7wsx0QuJCTui3zDlIqdXWVyXQHithyowE0bjEmXYNxkw2Pxn1AkVzNs0pl6GXZufATPdK/h8YMa9zwjymPt9VC1wXgbLmZHa+cr5nuvRzhm5JP7Smr4FhTe6fQWOmriyl6yjl2a2cWPqPM1ichPnBJbyxU6geFuwrSWWj/YFAI6xbH2JVTfJFXdJfg32BT8UTI4PuC2GsLGi8K9CMcsHU7S4eP8ckTM2S7n4fnKVpVrS2sjSEfgw6LRBy1xGmLqo1u6jygPG4pOyQe95bnnyX0mVe2cJIsU5oV6O1NRbdK/4YKfN0MGXjY4vFpvXNp+5A1r9rv6DyYRhd/vs2EZYPR3hWi42HxKTBvO1YzF21YXco71ayoVwpzpZaN9QP+9ab7D4dHgUrOzcfEH0VN4ABayM0NtcTFQ6LiA1J0WiyqO+lsGtFfcuO0bu28GroHx8aWTiw7sVg7ZxPhmX3v/yViPiTyYhgYqIBmgu1hrV3DddWC9g/p2m1YIBS6cHHa6OtOPjI6qtmGBzKTSp97dHnvo/47OBtmzrFvFfGxlRkZbaOpXWyLOqPbiSRVProMKatata/QaZqkiXdPtO5mDJrZrpXFlW9ZqLQSJksN68zXyZlSGWvSD2pV5SqZiuXwX96xVAN5ZG9mAAAAABJRU5ErkJggg==",
  "1": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAABs0lEQVR4nN2Uu4oUQRSGv1OnemeGFVYGM/EFRHMxMFFZFHwFH8DYUDPZQBZMDBQjN9VMMzEwNBKMBXEyQdhEhdmdqt+gLzNVPfsCnqC6ur4+/7nUoU2szVRvOpu+eIqOAIhiE4bZWwnp+jwyhx7Gdj1YGIcYwDFNqfnqPZLCHXYBvJQVWnCNXf5++70N7rEPH4S4MMDQ8z/ckG4Z4F7GFIroHghNzzOSnR8LfW6PR/CHODrgDCjePOF+CYeE7MHXR/xkiz3/wveP3LxSyVpLn6X8eBlXlxYmA5ms9J4x4+IZMVmxqqOtYQqphnHY5XGeoXirPgjeXW3TjGFMXaeygFSGCmRrAHK2pqtgyDp4owwwaZSLuQJin3/OUHatzVYA5oQaRkDByEs4pRr0CEgAPmpQ1YT/BCbGV9Y+moyfOJMaJoCUgTSehB3cLW/KngyeCdIgW/0Nupg5AYnTrbCzJWxeWwmbw18G8PBld+Dd4kDg9SdJejfdAh3u7p27vFEMQHTfIZhzlcntCfs9tOAe+6Jx5m30tXcn6YGGiOOt3rrxAmUyK0Bki4APxVgworUOFsH8H2kHsZnzBdU/AAAAAElFTkSuQmCC",
  "2": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAADC0lEQVR4nG2TS2icZRSGn+/2X2YmM5lcZGKihiporSJK7FTiBUMvA14WhVrrproICJVCF6WgK0E3rhTEhURBxWJ1qWBBuhKkWaggrQQLLgqGgCUhFfOPncvr4p9JJmnezcf53++c/z3new+SJP2ObgMCbCEsIt0TRhmfnr6rXJrhBYYRrwGWIFQEJv7Nc06/To1FAcBL5xBDZOrF0q8hrvciIWEQvPx3Jr0Dqtd4Qja/y90IUPmBgjGXBYsrZEsAaS4sEcLFFFMhVRkBID3ba4TmwdQTc/hZBFUACq8M57UfPoQBS/1PNIQHoFR+a/VHqbNw0k4V8fC00BhggGI380lyy9pVjGsDyGhkrSfV5EfAOQj4OSFjweZ9AtChI2jRHjsHKlJgAMYGAw4W0Le9zgfgAMeLv6Ap9uxGGr5ElyrD0S5kEf5Dx2K4uoO0MM7cccTE/jsF3lmzJclz75PHUWeWfVdEPq+B1E8n0E2oPy7mSXMZeSvWsox+K7jARbFOgvUAxoUA5cYxVOPoIcRzjxJD6I8wPXHhQfTJASA3XxpcwVsDFGtv1kCytXJDgrf7nVsY5wxI78MkDXFmLxhwndDybeJG9I2+O5/dvHrf9euYiw3eAyIi4OPqZ0jAAfa+ITgdgXGdYjNqIubDR2r89VBzaWkjNc8/c5b+IwdqJyro3X0TwJpouDkPYI11JGnlC/QHs1D/SXDhMNWeP1KOOk4imHkKia9eLUCFYMETOPLB50h33D+DBI8Nw4gPARz4I/lIGLsiMTsGkPStN58ifT3FqQ8FkpY319cazqMblGJocjtaKCWO8lUchPdtmtCqrHs4tf/SD3N20zrrYBHK923E7qgZIEOULAY7hMG7PvAt8EBLiG4X0dG21A20aSQ3WNUnpRs/s9PcfbIrv4DZlcOwZzn5/vLBLItXHXQHuRVDdQ0qMv+ExK2NbiNTk9zqEqavVdiITUY3/4ExxhgziY+BydEohs23wELwtrylDGON2SHNbIWOCAskjmgbZQS+XVspZ638wyND5eh/4z5BPu1u2ZAAAAAASUVORK5CYII=",
  "3": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAADAUlEQVR4nF2UTYhWZRiGr/d9zznf3/ypjfOn6ExaVFSCEVQSNIrQomgj5WawaNEyZlGb/qgYKNNgFrMIIigSghQtChcSggwIRkREWbmwcuMMM4XOiI0z39XifOP8vMtzcZ/nPPd73ycIEGTdKZ9kiVkRHt/FvDq4GxL/idShhng9z3ngsqD+3bsRxNEGIU6IwF/KIwJe3cPDF2WmCGBwqod8IXD62vwhCO6/Wrlg8EdAJBHF9xP0i31sZEH+KGE1FJk4RIWaePHRtlQXoTqNqvLLAJAOi1RTEKH9jYlytem3ep6iNaYzIkKRf14qj9JgGdafUIREAmLeDn2MwydiX3x1hBP/LEMoIAMaUyi8LT67AjN6gbaD4tM8+WLpzDKELjh8WRB+dj08paW77BUcXQXzjwV0civMia+Rr1KKegaqO5XXAeLaK+4d47mb/xKW2oeOEdcqizrALnEsdROyNcqFhWwxpM3nDN9nr6w41LKNLCtgn3JqHNZ/kD8A21G+fW81PCqAnt9CH8qbL63AwPFzLRdO0th9zNUzIzzzUM9xBTsCD66BiX30A1+KFPdsXQdhZ55zl3gROi+VxucppRgikW4CvCCSutokA24BxCZNphHagdS8sQCwgYJaEQo6CJ1UoSZuDySBDl4GYjVwCCotN3YAAo0vyvA5yr1QoeMbsQ+qAmPL3fzzK0YgVX7FuS1ZmPK2pSqyLbF5QuyKDIogniGA+M6dOzgt2g9lwib3IG2Rm61XgAxzQDwB94tXgG7L4epje/uVa0Dx6dc4m1fubs2+QR1+l+mTI5DqH4o593FH3pnBJjigfHcEqHUUiHYP9cOmwYENwBWZOQsBanD+N3SeVtgO/iSXzjJAJATYPz4J6vUqtWkV3wXIW+58sFh2QQUuUO0smxxiVoSc2cWVVY5QjVTJVwoxzEeqfjZcCzmJoqwDhMjz0AVsywIQgBRCIC2V/8jYbGtkM/lcvgCQLynLyhgSRAiE23Fr7abQjAhZM96iFQNIa3vWOikA/A8JdIqTXZZXpwAAAABJRU5ErkJggg==",
  "4": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAACOklEQVR4nM2UTWsTURSGn3PvnZkktkka29BS6geFVvETN1bQgqgLwaW4EHEnIrjU/1GxXatIwa27QjfiRhciKKVQUVxIQZTaKIitmHldTJJmEn+A72q4D+95z7kfY6IjU/+XB8YYfiyEAewSObgbhFpLtTZ0AAxs1JcBuwAJpG2ba5kXIxMsQwFqO23gIUFojcGJ8cEqDOUzwwshxsua3arBcFdmE5Ky6VOyCc+TP+RVD0+EGAEhRnNlqZWFvrOXb0JcZv8OLHFlbgVR2YNABMod6Ipcbc3/SCBKVHecYboVdV4IkZB0Z669QrPMSAjFUeiCSSxUP8dvcVco9j5uw2CVOnBxlcg2X/fMiI0IrRuNLDHnhAUhjiJ0ox8KiZklZVPm4aJQlSDsadwLwwcTjSNvIT1srrchIaanhO5g/8j8WqQhNGiEfugN8es23plQ8C4Wwpx3TitNRVCo4lMBTdIYwKXNFJ1K+CKUCQETQnpoAVdb3UaGZQJuXqqA2ebcH3AUeKOO1oGwJAlFQDQ1ic9mjqNSGD50ndLo6VbDtziRPRGAaxyLotKBswglDhKOh2TQ4UJc5AwHi8xme1PGCAPpD3DebBtgdMxtfN5CJgPl9Q7goyRkUejEtfarcvLn+30GLx/I9ziRg7qQ7g+Vfd8RRdlDukdPScicBSFw7cfbpRRiYJKQY5H3WdlS55p0eZoAzxrpGIDr/D8A8NlN7JxczgksKOtRC72teGA+c873MnzfSt8o/zX8C6i0OtvT8AGWAAAAAElFTkSuQmCC",
  "5": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAAC90lEQVR4nGWUT2gdVRTGf/fcO3/y/uQlb2LSJKUtaG1CEV0IWgm4EsGF4qKCC5VCcSO2KyUbcdGCrkpLQQQXulALRXAhuAvUjUt9dqcWXQhSQWIMeal5vJnPxZ15ebRnM3Pvd/59Z74zDvGAucnd/eA6au4MCD1OnJ6AWp28BkD6u3gwd53Wzd+XWu9N1TwiNG13SIPVackBNxW5yIhQ1ZGK/Xmcm/ikdbcOp6sdKEEOwDd8kbPSKZR4PzdmWI7Bl5COwHRABZSQZen23sFyZtNMQIhXyfxKt/cKQGKOdAqc2MfzHvBNQ2bgJvb9+yWOcjIgm45k6/qMqyMRxo97P0XPY89I3NrIEyMFnJxVVrHeTX4d7QDb8+7W7QsQxjEtgIbSyQI4syVEkadmIIOOJJCkt1hCaDMhxyKVO3G+oL1Haf0m9iFrQKEBX0tCe6+B0C+8EbstloRePnGKU58Jpc/xkfjnichTkvj8dWARoY02y0LQamasAW9DitBtyIS+Kgh15NOt6PSuEIT48XNjDUebJefBeYTawdajMsyMzIbhLzNQOQe4cTUE6IYoLlxdPBGahVSIGbD/kgDt6uFaNCCoah0Z5Rjf4Qi9ECdi0SdPgtlZn1PepWBfM6wCWRTzcHeM3Rzdk/6g9+JCeY/zu1Cmfh925zqNQC//EJ+fCMV+bq4dju/PF3ioVhrtZaHC94MF58v0gOVvO/lTjz8COIbX4bIrd6qU4yleSE92uSK09SyIizd4JxgM8NboANQ9l0poI4mrsjDbP3NJivWvZSD0BeADaLTCJuiaJK114j7OWr1q0nGg2Hzz5Fyx+hjb4u6No/VyXuWcFvoLNeGV78TgG5rj7/8iqQPY/FFJ/HwJgCwD+GCHw1WBLz+kFciaDb0yOPzTSIIUkngM8Pynk8iXkngVItjnNPliZ+Nsr0vWZslh1nwPZun4MANYK+tnHMM5nKUxcZskEs7TugqAjzPIcBiGi7z9NBjGCIevkjIKN6K+tNLc/3UZZMZ7g8UiAAAAAElFTkSuQmCC",
  "6": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAADHklEQVR4nE2TTWhdVRSFv3Puefe9+5KX5CVpS9q0SQgxfWkK5impChaL+NtJhjp0Ig4ciCJ04kAcKupQBEcWf/CvdlAnKoggVIhQlE6EFtFSKU0bgiaSNvkcnJc2Gy73HBZrn7X32jsIQMi/ndh1vSVeO74YagcP1vdAxVX5/VwPFP9mFnPMJlqK8DCwJr44ojmVyqPPiB/Sw+SAZCoIZCqI9h9W9K2Xjyr67ALixSaIcFiUqXH4WBzK1EdYEWkiPsHiXC+TKhdA3GQY8f42QKsnDH9tIu6nVvQEMAhfZubZKj/ZqJriwhnajFIx2GiP7h8JrQyWBHH6OSDyCkAoE30Z7E/FjoyspRkhQAT4DyDciePvl41Uq8UKkXqt3M3Eb4GQAO22a5xf+2XHpL/EVWKVAggDBRR0Fh8cPnDs5CQ/ip8O5WI77ZFmDXBFr01BwUW5/B1EMNFoBFbNbrjESRQbIYLQV3EpWwZK/xLiF0OA0Kpyu/lG0RLWd/xcpC9XBJPDr4sc6u/d2ThOM4MP9NfnEXkaxA5gl6oZROg0WBBfWoxIFgh9MYow0Q2cEY/kR+eyoFSIFABRnIC9IhHu4R8A9rY7LeoRqMF2buW2oUyFGBkuoBRnoBCJRtzcBtjmFtCg5yOUEaAmQOq7HWAyg1vAQMqZAW5HgJXengGTcffiFaHgI6CEVeAquYZCDAwTEc8eKRGXE6Jtovh9mNg3/oE4222TW4E4WsYbcp1jtMSfaRxaubtl4zldHi55kjFx5hSwiut5IwG8fIKlqbUesTolXp9vz+ex/IFOF8Sv4HHgighdSNPTR8PAKM8vi/251iD67iKQyvbM5BiK59oQGIK381zVa1UDGPhJbnySEvQBY9xU1N9gLSt+A1Iqcvfe+9rlPLQCvPMaJPI3xeDEC36eAdUL+2Ko9/p9cHSqOfYZPHZafbMFFIk6sSgAmCo4cRo4wH33DgGxlutITw39e+nKFucfAq6wTB3YLlJtI92+a2bnVVLvWISQ/b1j9p9/sKdZwUAIW0pJSoZqozdNm8XcxtbNVWhsEzcb6xT+D7ybtfi4iH72AAAAAElFTkSuQmCC",
  "7": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAACWklEQVR4nHWUO2gUURSGv/uY2dmd2WSTDZisWUHjA7EQFYs0GgJKKkVEjJVYq5VliqSyUkJsbHxUYqN1QCwMpJEgKaLY2AVEBFHRIrAhv8Xd2benuMX553+cc4drxKAyod0LRljnhFjoA8eIIwdClIh8H9VUfwDQAG96MGEoAPhGg/fqKkTKVyEchrQYGDaqcUpoCWbFnU08LVHX1BQ3OSA002nlGD7BJ6GL8Es9Ka0LxJ1aZVJoifHhrFP2Rl0ogk0hKNm2p4d0A/0tUxRaphUUIIG7QlGZ3eDYxtI4OIo4FnpO0ZdaYBF3/5UQTP4WOjQxlNCxunnW0ctzvix06Xp7coAHPBZiHoSOtmYLdQahhYQ5hGAaTBu8dlqIMWZeCEGl0CHr+YK28X5a6Iml0ulZrAmBS8tCmXfdgVbC5PvCzi00D4BMaG8RpoTU/DtMnvfpRwSUJAQ9f47Q93KGBFuHk3bfOXdsVYjqSNjueDfvwkEkRmq00gwFwAJTq3AS820ZINnrIk7c+owwaU3omXVJF1h/exXFBbaFihE+jGENEHNZSIBorFUc1pdwrSWsC2VkqRDVCIjJ11dYEXozSumnuE1CF1gRUv0sEmsP7+HBJq05RoWuMIu4/SG0PFgXAr0WmjvizwvNQRLnqvlat/czI7RYjMdjm6sCGBn57HjhnZHBpn/i3Xin4561NxTEYRh873XFpir0CCqUMS7qwETswiUT9RGbT8HKgMcjr60Qr6cbmBt1ILL9VJOfdjACDqS+R9JFzS8GpnFgzEA/cqc+v6arddF/RvwHnmr0+zvp28kAAAAASUVORK5CYII=",
  "8": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAADBUlEQVR4nGWTT2ucVRTGf+fe+973nf+ZYUzp2NLWaKhWgjFoEBQX4kKC0ApiF136EVy5EVy4EBcifgI/grtCUOgHENKI0baotaaNJrXRxIyZyczj4r6TxHhW557n3HvPec5zTJg4bsePQpuuzulmpVq/8B4vbArdid5ZQgXdLyVJetBaavwoBDiAFVXYUvmUdlssCW1A5gy05BCkm4hXkFAGeGM1xwsJDKF1FhDaMAOPRXz5i/8uOY+E8oABniDkAw4SSHrX44ADoGpY2VwHYJhjqWKgXbSTG6msAQw4OATP77ghKbh/0QQej2MEwBVCynqLLaDHa4D7Nf2xXfwNd4AbADw4t+JHoHAukbBxV6nfm0IXunQAbV9EUuJYn4KEvmlWQsSlIHcT/DFvrgt97SiaZd97C3NcXZaGFi5LaK1WsyyUE5O6zXqv1y7Jue44U80LDKYkxDyLvAQ8J6TfaXozCEh0r77D9PwTswbTaoKWIc8mUvgEfpF0+/wbT74uCd3CWwAkcUkSIH20SIwCfZvoWqHBVpICSDqbvSihKj7ATKOFkMbzEyoCQtvEALiI0LvAQlKRY19oBuBxX3NCcI053hbKXFYXeh+AwiFEHpmx9pFOzgAuHqR5D3N2svHRJtQBN/AJtDF/jv85vibgDoUi9osMDnU2WYlJatz9T+TIHQ3hIAK58wA5MR6Crg1+CPTHI4A+g4GjrHAc4NENIMIu0CLzVMkQ+rxyNlyT0D2oCS33AOSa/CZ0u8eHQvfoTf0sRDQD5RlcF2k9v4fnERKdApC6RZEkiPamu1z6TIhFiiTN2KHxhyStT82+jIR+8rhWAKEPPM/w1NKrzz7tmF0VegguM19K8weqAJ2wIKEv5j3m8eBLedyH1VTU+gKAdxkQ10p1lUXtALXCewAq8NUEkKQWFJZnOeaIHlhlT5JukRV0AxkG5srpmR7bPHV69PA+czerw2HNXF+5DSwOLB8Nqfv+gFN/hUZ/Nx/uc9IcRMAILhrR+4iZIf0v84T5E0dzGf8CloOKnKruyuQAAAAASUVORK5CYII=",
  "9": "iVBORw0KGgoAAAANSUhEUgAAABwAAAAyCAAAAABqwOQOAAADGElEQVR4nF2UT2hcVRTGf/e+N29m3ryZpkmaxGDUVNPaUEWrRrTQldUUF24UC0VcWSiCrlx1Z11aquJOxBBxFURdiisXCnUhaRYGQmhBhBoqremfNM1k8nPxJunEs7r3fvfj3nPO950gQXqjd7uC8hycAHgDlkRISABQ4KKqDvAi4mfEEvzAl4Yf0e494ZoszkAEkOahTVEVrB/lsggEYJy+IJ6bgouiY5wV6acCDOX1msiRYR6bE5+NL4u/vUoN2E81FxnLqOXiEUa+l9X3AWgQGiL7MpgQOQ0iZ7Iy1UKkyOBhkeepiWNUY4i9xen8Db8vcg8oimyLtPwzEAh3rxhs3MyBRfoDQNoSKapAFPdTF3+kBils7pS8tQo8fjnBIIEQoVa+K24A/Es63r0cM9a3AGISaOwDrsE6wBYhblARINytUKsBf9EGYBPiSdIOUGRkJN8FV9fpALAVAlAU4nwtezNeF1OojYgQ4Wmo+jYeBG7gr6PQGOmqgZ8hfUVxwX9Epgdp7oC2aNaDouBshaIXlAezyC1LlbSmByl2QNQPX9/LL7qMenVvl5lUSH8IuDAXXjhXWVuSgK0bZSc6HdJJLwVwW9CBQGh2ix0nxm5q6MZWebjUo//4ud1oiwO0xHcJ1S4+QOuB2BzoOy0WNMVPHwWo15uhqDJVGajU+8tsB0tVxxCTJMlv32NPu9O+twGcOsYawFcQSh9scPL4AYp+MYEFuVRvEkujMbcMGVwQP3rmqPgl7xCTroNWphjMEOdB+RMgSYC8T5RDpy6IjUlk+ZN6kRETJGFF1DVxbRTl1lCVJMQ0Qicdvm4IIQ/+lE9C8Hx7MyYxbuICcGJe1dvHOTwrkielB9DDHMyBfBCisvA1OSFLUriKM2+xZ5pai2ziNXE2JYdIiJz5GPWp4fGhLKtUFalS69b82Lyg7z0Bf6j4LfcbAl9Y6kcB01Eg6zHt+ftzaObJZhJ2gfCN6tpZhsrhtAt8CBpwoMEEdSrJNlh6M6bcyeOVOySBanv3FAWoAFAnwG5mSAmdEIAO8f+ssHsdt5n/Afu9eV7nvj7SAAAAAElFTkSuQmCC"
}


def _get_templates() -> dict[int, tuple[np.ndarray, float]]:
    global _TEMPLATES_CACHE
    if _TEMPLATES_CACHE is None:
        templates = {}
        for d, b64_str in _TEMPLATES_B64.items():
            raw_bytes = base64.b64decode(b64_str)
            img = Image.open(io.BytesIO(raw_bytes))
            arr = np.array(img).astype(np.float32)
            norm = float(np.linalg.norm(arr))
            templates[int(d)] = (arr, norm)
        _TEMPLATES_CACHE = templates
    return _TEMPLATES_CACHE


def extract_first_frame_crop(video_path: Path | str) -> Image.Image | None:
    """Extract 1000x60 top banner crop from frame 0 via FFmpeg in memory."""
    try:
        cmd = [
            "ffmpeg",
            "-v", "error",
            "-ss", "0",
            "-i", str(video_path),
            "-vframes", "1",
            "-vf", "crop=1000:60:0:10",
            "-f", "image2pipe",
            "-vcodec", "png",
            "-",
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        if not res.stdout:
            return None
        return Image.open(io.BytesIO(res.stdout))
    except Exception as exc:
        logger.debug("Failed to extract frame crop from %s: %s", video_path, exc)
        return None


def read_osd_time_from_crop(crop_img: Image.Image) -> tuple[str | None, list[float]]:
    """Match OSD digits and return recognized HH:MM:SS string and score per digit."""
    edge_img = crop_img.convert("L").filter(ImageFilter.FIND_EDGES)
    crop_arr = np.array(edge_img)
    templates = _get_templates()

    digits: list[str] = []
    scores: list[float] = []

    for _, rx0, rx1 in WINDOWS:
        best_d = -1
        best_sc = -1.0
        for x in range(rx0, rx1):
            for dy in range(-2, 3):
                sy = max(0, dy)
                sub = crop_arr[sy : sy + 50, x : x + 28].astype(np.float32)
                if sub.shape != (50, 28):
                    continue
                norm = float(np.linalg.norm(sub))
                if norm < 1e-3:
                    continue
                for d, (tmpl, tmpl_norm) in templates.items():
                    sc = float(np.sum(sub * tmpl) / (norm * tmpl_norm))
                    if sc > best_sc:
                        best_sc = sc
                        best_d = d
        digits.append(str(best_d))
        scores.append(best_sc)

    if any(d == "-1" for d in digits):
        return None, scores

    time_str = f"{digits[0]}{digits[1]}:{digits[2]}{digits[3]}:{digits[4]}{digits[5]}"
    return time_str, scores


def parse_filename_datetime(filename: str) -> tuple[datetime | None, str, str]:
    """Parse YYYY-MM-DD_HH-MM-SS from filename stem.
    
    Returns (datetime_obj, prefix, suffix_with_hash).
    Example: '2026-10-02_04-13-16_0_b985444030'
    """
    stem = Path(filename).stem
    m = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})(.*)", stem)
    if not m:
        return None, "", ""
    dt_str = f"{m.group(1)} {m.group(2)}:{m.group(3)}:{m.group(4)}"
    try:
        dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        rest = m.group(5)
        return dt, m.group(1), rest
    except ValueError:
        return None, "", ""


def detect_video_preroll(
    video_path: Path | str,
    event_datetime: datetime | None = None,
    min_confidence: float = 0.70,
) -> tuple[int | None, str | None, float]:
    """Detect pre-roll seconds and visual start time for a video file.
    
    Returns (preroll_seconds, visual_time_str, min_score).
    If detection is invalid or low-confidence, returns (None, None, min_score).
    """
    path = Path(video_path)
    fn_dt = event_datetime
    if fn_dt is None:
        fn_dt, _, _ = parse_filename_datetime(path.name)
    if fn_dt is None:
        return None, None, 0.0

    crop = extract_first_frame_crop(path)
    if crop is None:
        return None, None, 0.0

    vis_time_str, scores = read_osd_time_from_crop(crop)
    if not vis_time_str or not scores:
        return None, None, 0.0

    worst_score = min(scores)
    if worst_score < min_confidence:
        return None, None, worst_score

    try:
        vis_t = datetime.strptime(vis_time_str, "%H:%M:%S").time()
    except ValueError:
        return None, None, worst_score

    fn_t = fn_dt.time()
    fn_sec = fn_t.hour * 3600 + fn_t.minute * 60 + fn_t.second
    vis_sec = vis_t.hour * 3600 + vis_t.minute * 60 + vis_t.second

    preroll = fn_sec - vis_sec
    # Handle midnight boundary
    if preroll < -80000:
        preroll += 86400
    elif preroll > 80000:
        preroll -= 86400

    # Plausible pre-roll check: between -2s and +15s
    if -2 <= preroll <= 15:
        return preroll, vis_time_str, worst_score

    return None, vis_time_str, worst_score


def sync_recording_filenames(
    video_path: Path,
    thumb_path: Path | None = None,
    event_datetime: datetime | None = None,
    min_confidence: float = 0.70,
) -> tuple[Path, Path | None, int | None]:
    """Detect pre-roll and rename video and thumbnail to match visual start time.
    
    Returns (final_video_path, final_thumb_path, preroll_seconds).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        return video_path, thumb_path, None

    fn_dt = event_datetime
    rest = ""
    if fn_dt is None:
        fn_dt, _, rest = parse_filename_datetime(video_path.name)
    else:
        m = re.search(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(.*)", video_path.stem)
        rest = m.group(1) if m else ""

    if fn_dt is None:
        return video_path, thumb_path, None

    preroll, vis_time, score = detect_video_preroll(
        video_path, event_datetime=fn_dt, min_confidence=min_confidence
    )
    if preroll is None or preroll == 0:
        return video_path, thumb_path, preroll

    # New visual datetime
    new_dt = fn_dt - timedelta(seconds=preroll)
    new_time_str = new_dt.strftime("%Y-%m-%d_%H-%M-%S")
    new_video_name = f"{new_time_str}{rest}{video_path.suffix}"
    new_video_path = video_path.with_name(new_video_name)

    if new_video_path != video_path:
        video_path.rename(new_video_path)

    new_thumb_path: Path | None = None
    if thumb_path is not None and Path(thumb_path).exists():
        thumb_path = Path(thumb_path)
        new_thumb_name = f"{new_time_str}{rest}{thumb_path.suffix}"
        new_thumb_path = thumb_path.with_name(new_thumb_name)
        if new_thumb_path != thumb_path:
            thumb_path.rename(new_thumb_path)

    return new_video_path, new_thumb_path, preroll
