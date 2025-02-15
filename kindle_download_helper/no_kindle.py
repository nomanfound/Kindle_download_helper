import base64
import json
from pathlib import Path
import re
import shutil
import time
from collections import namedtuple
from datetime import datetime
from enum import Enum
from io import BytesIO
from zipfile import ZipFile

import requests
import xmltodict
from amazon.ion import simpleion
from mobi import extract
from rich import print

from kindle_download_helper import amazon_api
from kindle_download_helper.config import (
    API_MANIFEST_URL,
    DEFAULT_OUT_DEDRM_DIR,
    DEFAULT_OUT_DIR,
    DEFAULT_OUT_EPUB_DIR,
)
from kindle_download_helper.dedrm import MobiBook, get_pid_list
from kindle_download_helper.utils import trim_title_suffix
from kindle_download_helper.dedrm.kfxdedrm import KFXZipBook
from kindle_download_helper.third_party.ion import DrmIon, DrmIonVoucher
from kindle_download_helper.third_party.kfxlib import YJ_Book

DEBUG = False
DEFAULT_TIMEOUT = 180
old_send = requests.Session.send


def new_send(*args, **kwargs):
    if kwargs.get("timeout", None) is None:
        kwargs["timeout"] = DEFAULT_TIMEOUT
    return old_send(*args, **kwargs)


requests.Session.send = new_send


class Scope(Enum):
    REQUIRED = 1
    PREFERRED = 2
    DEFERRED = 3

    def should_download(self, s: str):
        r = Scope[s.upper()]
        return self.value >= r.value


Request = namedtuple("Request", ["method", "url", "fn", "headers"])


def _build_correlation_id(device, serial, asin, timestamp):
    if timestamp is None:
        timestamp = datetime.utcnow().timestamp()
        timestamp = str(int(timestamp) * 1000)
    return f"Device:{device}:{serial};kindle.EBOK:{asin}:{timestamp}"


class NoKindle:
    def __init__(
        self,
        email,
        password,
        domain,
        out_dir=DEFAULT_OUT_DIR,
        out_dedrm_dir=DEFAULT_OUT_DEDRM_DIR,
        out_epub_dir=DEFAULT_OUT_EPUB_DIR,
        cut_length=76,
    ):
        self.out_dir = out_dir
        self.out_dedrm_dir = out_dedrm_dir
        self.out_epub_dir = out_epub_dir
        self.session = requests.Session()
        self.ebooks = []
        self.pdocs = []
        self.library_dict = {}
        self.cut_length = cut_length
        self.book_name_set = set()

        print("Authenticating . . .")
        self.tokens = amazon_api.login(email, password, domain)

    def decrypt_voucher(self, voucher_data):
        with BytesIO(voucher_data) as voucher_data_io:
            for pid in [""] + [self.tokens["device_id"]]:
                for dsn_len, secret_len in [
                    (0, 0),
                    (16, 0),
                    (16, 40),
                    (32, 40),
                    (40, 0),
                    (40, 40),
                ]:
                    if len(pid) == dsn_len + secret_len:
                        break  # split pid into DSN and account secret
                    else:
                        continue
            voucher = DrmIonVoucher(voucher_data_io, pid[:dsn_len], pid[dsn_len:])
            voucher.parse()
            voucher.decryptvoucher()
        return voucher

    def decrypt_kfx(self, kfx_data):
        if kfx_data[:8] != b"\xeaDRMION\xee":
            return kfx_data

        with BytesIO() as decrypted_data:
            DrmIon(BytesIO(kfx_data[8:-8]), lambda name: self.drm_voucher).parse(
                decrypted_data
            )
            return decrypted_data.getvalue()

    def get_resource(self, resource, asin):
        resp = self.session.send(
            amazon_api.signed_request(
                "GET",
                resource["endpoint"]["url"],
                asin=asin,
                tokens=self.tokens,
                request_id=resource["id"],
                request_type=resource["type"],
            )
        )

        filename = resource["id"]
        if resource["type"] == "DRM_VOUCHER":
            filename += ".ast"
        else:
            filename += ".kfx"

        return (resp.content, filename)

    def make_library(self, last_sync=None):
        """Fetches the user library."""
        url = "https://todo-ta-g7g.amazon.com/FionaTodoListProxy/syncMetaData"
        params = {"item_count": 10000}

        if isinstance(last_sync, dict):
            try:
                last_sync = last_sync["sync_time"]
            except KeyError as exc:
                raise ValueError("`last_sync` doesn't contain `sync_time`.") from exc

        if last_sync is not None:
            params["last_sync_time"] = last_sync

        r = self.session.send(
            amazon_api.signed_request(
                "GET",
                url,
                tokens=self.tokens,
            )
        )
        library = xmltodict.parse(r.text)
        library = json.loads(json.dumps(library))
        library = library["response"]["add_update_list"]
        ebooks = [i for i in library["meta_data"] if i["cde_contenttype"] == "EBOK"]
        pdocs = [i for i in library["meta_data"] if i["cde_contenttype"] == "PDOC"]
        ebooks = [e for e in ebooks if e["origins"]["origin"]["type"] == "Purchase"]
        unknow_index = 1
        for i in pdocs + ebooks:
            if isinstance(i["title"], dict):
                if i["ASIN"] in self.library_dict:
                    unknow_index += 1
                self.library_dict[i["ASIN"]] = i["title"].get(
                    "#text", str(unknow_index)
                )
            else:
                self.library_dict[i["ASIN"]] = i["title"]

        self.ebooks = ebooks
        self.pdocs = pdocs

    def sidecar_ebook(self, asin):
        url = f"https://sars.amazon.com/sidecar/sa/EBOK/{asin}"
        r = self.session.send(
            amazon_api.signed_request(
                "GET",
                url,
                tokens=self.tokens,
            )
        )
        print(r.json())

    def sidecar_bookmark(self, sidecar_url):
        r = self.session.send(
            amazon_api.signed_request(
                "GET",
                sidecar_url,
                tokens=self.tokens,
            )
        )
        try:
            print(r.json())
        except:
            print(r.text)

    @staticmethod
    def _b64ion_to_dict(b64ion: str):
        ion = base64.b64decode(b64ion)
        ion = simpleion.loads(ion)
        return dict(ion)

    def get_book(self, asin):
        manifest_resp = self.session.send(
            amazon_api.signed_request(
                "GET",
                API_MANIFEST_URL + asin.upper(),
                asin=asin,
                tokens=self.tokens,
                request_type="manifest",
            )
        )
        try:
            resources = manifest_resp.json()["resources"]
        except Exception as e:
            print(manifest_resp.json(), str(e))
            return None, False, str(e)
        manifest = manifest_resp.json()
        # azw3 is not so hard
        drm_voucher_list = [
            resource for resource in resources if resource["type"] == "DRM_VOUCHER"
        ]
        if not drm_voucher_list:
            return manifest, False, "Succeed"

        drm_voucher = drm_voucher_list[0]
        try:
            self.drm_voucher = self.decrypt_voucher(
                self.get_resource(drm_voucher, asin)[0]
            )
        except:
            print("Could not decrypt the drm voucher!")

        manifest["responseContext"] = self._b64ion_to_dict(manifest["responseContext"])
        for resource in manifest["resources"]:
            if "responseContext" in resource:
                resource["responseContext"] = self._b64ion_to_dict(
                    resource["responseContext"]
                )
        return manifest, True, "Succeed"

    def download_book(self, asin, error=None):
        manifest, is_kfx, info = self.get_book(asin)
        # TODO for bookmark
        # for r in manifest["resources"]:
        #     if r["type"] == "KINDLE_USER_ANOT":
        #         url = (r["endpoint"]["url"])
        #         self.sidecar_bookmark(url)
        if not manifest:
            print(f"Error to download ASIN: {asin}, error: {str(info)}")
            return
        if is_kfx:
            self._download_kfx(manifest, asin)
        else:
            self._download_azw(manifest, asin)

    def _save_to_epub(self, drm_file, out_epub):
        try:
            # save to EPUB
            epub_dir, epub_file = extract(str(drm_file))
            shutil.copy2(epub_file, out_epub)
            # delete it
            shutil.rmtree(epub_dir)
        except Exception as e:
            print(str(e))

    def download_pdoc(self, asin):
        """from mkb79/kindle Downloading personal added documents"""
        url = "https://cde-ta-g7g.amazon.com/FionaCDEServiceEngine/FSDownloadContent?type=PDOC&key={asin}&is_archived_items=1&software_rev=1184370688"
        r = self.session.send(
            amazon_api.signed_request(
                "GET",
                url.format(asin=asin),
                asin=asin,
                tokens=self.tokens,
            )
        )

        book_name = trim_title_suffix(
            self.library_dict.get(asin, "").encode("utf8").decode()
        )
        # we should support the dup name here
        name = book_name
        if book_name in self.book_name_set:
            name = book_name + "_" + asin[:4]
        else:
            self.book_name_set.add(book_name)
        azw3_name = name + ".azw3"
        epub_name = name + ".epub"
        content_bytes = r.content
        if content_bytes[0x3C : 0x3C + 8] != b"BOOKMOBI":
            print(
                f"Book {asin}, {book_name} faild first content {str(content_bytes[:100])}"
            )
            self.book_name_set.discard(book_name)
            return
        out_epub = Path(self.out_epub_dir) / Path(epub_name)
        pdoc_path_drm = Path(self.out_dir) / Path(azw3_name)
        pdoc_path_drm.write_bytes(content_bytes)
        self._save_to_epub(pdoc_path_drm, out_epub)

    def _download_kfx(self, manifest, asin):
        resources = manifest["resources"]
        parts = []
        scope = Scope.DEFERRED
        if isinstance(scope, str):
            try:
                scope = Scope[scope.upper()]
            except KeyError:
                allowed_scopes = [s.name.lower() for s in Scope]
                raise ValueError(
                    "Scope must be in %s, got %s" % (", ".join(allowed_scopes), scope)
                )
        for resource in resources:
            if not scope.should_download(resource["requirement"]):
                continue
            try:
                url = (
                    resource.get("optimalEndpoint", {}).get("directUrl")
                    or resource.get("endpoint")["url"]
                )
            except KeyError:
                raise RuntimeError("No url found for item with id %s." % resource["id"])
            headers = {}
            fn = None

            if resource["type"] == "DRM_VOUCHER":
                fn = resource["id"] + ".voucher"
                correlation_id = _build_correlation_id(
                    "A2A33MVZVPQKHY",
                    self.tokens["device_id"],
                    asin=manifest["content"]["id"],
                    timestamp=manifest["responseContext"]["manifestTime"],
                )

                headers = {
                    "User-Agent": "Kindle/1.0.235280.0.10 CFNetwork/1220.1 Darwin/20.3.0",
                    "X-ADP-AttemptCount": "1",
                    "X-ADP-CorrelationId": correlation_id,
                    "X-ADP-Transport": str(manifest["responseContext"]["transport"]),
                    "X-ADP-Reason": str(manifest["responseContext"]["reason"]),
                    "x-amzn-accept-type": "application/x.amzn.digital.deliverymanifest@1.0",
                    "X-ADP-SW": str(manifest["responseContext"]["swVersion"]),
                    "X-ADP-LTO": "60",
                    "Accept": "application/x-com.amazon.drm.Voucher@1.0",
                }
                if "country" in manifest["responseContext"]:
                    headers["X-ADP-Country"] = str(
                        manifest["responseContext"]["country"]
                    )

                url += "&supportedVoucherVersions=V1"
            elif resource["type"] == "KINDLE_MAIN_BASE":
                fn = manifest["content"]["id"] + "_EBOK.azw"
            elif resource["type"] == "KINDLE_MAIN_METADATA":
                fn = resource["id"] + ".azw.md"
            elif resource["type"] == "KINDLE_MAIN_ATTACHABLE":
                fn = resource["id"] + ".azw.res"
            elif resource["type"] == "KINDLE_USER_ANOT":
                fn = manifest["content"]["id"] + "_EBOK.mbpV2"

            parts.append(Request(method="GET", url=url, fn=fn, headers=headers))

        files = []
        for part in parts:
            r = self.session.send(
                amazon_api.signed_request(
                    part.method,
                    part.url,
                    asin=asin,
                    tokens=self.tokens,
                    headers=part.headers,
                )
            )
            fn = part.fn

            if fn is None:
                cd = r.headers.get("content-disposition")
                fn = re.findall('filename="(.+)"', cd)
                fn = fn[0]

            fn = Path(self.out_dir) / Path(fn)
            files.append(fn)
            fn.write_bytes(r.content)
            print(f"Book part successfully saved to {fn}")

        asin = manifest["content"]["id"].upper()
        manifest_file = Path(f"{asin}.manifest")
        manifest_json_data = json.dumps(manifest)
        manifest_file.write_text(manifest_json_data)
        files.append(manifest_file)
        name = trim_title_suffix(
            self.library_dict.get(asin, "").encode("utf8").decode()
        )
        if len(name) > self.cut_length:
            name = name[: self.cut_length - 10]
        fn = name + "_" + asin + "_EBOK.kfx-zip"
        fn = Path(self.out_dir) / Path(fn)
        out_epub = Path(self.out_epub_dir) / Path(name + ".epub")
        with ZipFile(fn, "w") as myzip:
            for file in files:
                myzip.write(file)
                file.unlink()

        fn_dec = name + "_" + asin + "_EBOK.kfx-zip.tmp"
        kfx_book = KFXZipBook(fn, self.tokens["device_id"])
        kfx_book.voucher = self.drm_voucher
        kfx_book.processBook()
        kfx_book.getFile(fn_dec)
        Path(fn).unlink()
        Path(fn_dec).rename(fn)
        b = YJ_Book(str(fn))
        epub_data = b.convert_to_epub()
        with open(out_epub, "wb") as f:
            f.write(epub_data)

    def _download_azw(self, manifest, asin):
        resources = manifest["resources"]
        url = resources[0]["endpoint"]["url"]
        r = self.session.send(
            amazon_api.signed_request(
                "GET",
                url,
                asin=asin,
                tokens=self.tokens,
            )
        )
        name = trim_title_suffix(
            self.library_dict.get(asin, "").encode("utf8").decode()
        )
        if len(name) > self.cut_length:
            name = name[: self.cut_length - 10]
        out = Path(self.out_dir) / Path(name + ".azw3")
        out_epub = Path(self.out_epub_dir) / Path(name + ".epub")

        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=512):
                f.write(chunk)
        out_dedrm = Path(self.out_dedrm_dir) / Path(name)
        time.sleep(1)
        mb = MobiBook(out)
        md1, md2 = mb.get_pid_meta_info()
        totalpids = get_pid_list(md1, md2, [self.tokens["device_id"]], [])
        totalpids = list(set(totalpids))
        mb.make_drm_file(totalpids, out_dedrm)
        time.sleep(1)
        self._save_to_epub(out_dedrm, out_epub)


if __name__ == "__main__":
    kindle = NoKindle()
    kindle.make_library()
    for e in kindle.ebooks:
        try:
            kindle.download_book(e["ASIN"])
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(e)
        # spider rule
        time.sleep(1)
