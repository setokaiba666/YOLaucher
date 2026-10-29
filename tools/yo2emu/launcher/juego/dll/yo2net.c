/*
 * yo2net.c - DLL proxy "version.dll" para yo2.exe (Yu-Gi-Oh! Online 2, Ver.080912, PE de 32 bits).
 *
 * Sustituye a frida-inject + redirect_hook.js + aviso_sala_hook.js en el PC del jugador (rama rx/DLL, 12-10).
 * El lanzador la copia como "version.dll" junto a yo2.exe y abre yo2.exe directamente con estas variables de entorno:
 *
 *   YO2ONLINE_NET=1          enciende los ganchos (sin ella, o con "0", la DLL SOLO reenvia version.dll: no toca nada)
 *   YO2ONLINE_NET_LOG=<ruta> fichero donde se apunta lo que hace (el lanzador lo copia a su log al cerrar el juego)
 *   YO2ONLINE_OFFSET=<n>     solo pruebas: se suma al puerto 5730 (la puerta) y al HTTP 8730 (como net_hook.js)
 *   YO2_AVISO_SALA=1/0       el mismo interruptor que aviso_sala_hook.js (o el fichero yo2-aviso-sala.txt)
 *   YO2ONLINE_WINDOW=orig|1.5|2|fit  ventana ampliada (rx/PAN, 24-10; ver "ventana ampliada" mas abajo). Vacio u "orig" =
 *                            el juego tal cual. Funciona sola (sin YO2ONLINE_NET) y no hace falta nada mas.
 *
 * Por que version.dll: yo2.exe la importa (GetFileVersionInfoA/SizeA, VerQueryValueA), no es una KnownDLL (Windows la
 * busca primero en la carpeta del exe) y tiene solo 17 exportaciones. winmm.dll NO: es la del mod offline de derplayer
 * (winmm.dll.offlinemod). d3d8/d3d9/dxgi tampoco: son las de dxvk/d3d8to9 que ya trae la carpeta del cliente.
 * El mod offline importa VERSION.dll: si alguien lo activa, recibe esta DLL, que reenvia todo a la de sistema.
 *
 * Lo que hace con YO2ONLINE_NET=1 y el exe principal llamado yo2.exe (copia de redirect_hook.js):
 *   - gethostbyname("*.yugioh-online.net") -> "127.0.0.1"         (los servidores de Konami ya no existen)
 *   - connect(AF_INET, cualquier IP)        -> 127.0.0.1, mismo puerto (5730 + YO2ONLINE_OFFSET si lo hay)
 *   - InternetOpenUrlA("http://host/...")   -> http://127.0.0.1:8730/... (8730 + YO2ONLINE_OFFSET)
 *   Se hace parcheando la IAT de yo2.exe (WSOCK32 por ordinal 52/4 o por nombre, WS2_32 igual, WININET por nombre).
 *   Frida enganchaba ws2_32!connect para todo el proceso; la unica otra que llama a connect es WinINet, y lo hace hacia
 *   127.0.0.1:8730 porque la URL ya va reescrita: el efecto es el mismo.
 * Y el aviso 5092 al pulsar MOVE con sala propia (copia de aviso_sala_hook.js; apagado salvo YO2_AVISO_SALA=1 o
 * yo2-aviso-sala.txt junto a yo2.exe): salto en 0xa00c21 a un trampolin que llama a aviso_en_clic con eax/esi.
 *
 * Y, solo con YO2ONLINE_WINDOW distinto de "orig", la ventana ampliada: el juego sigue creyendo que mide 1024x768, la ventana
 * real es mas grande (1.5x, 2x o todo el monitor sin bordes con bandas negras 4:3) y se traducen las coordenadas del raton.
 * Las cuentas (puras, probadas con test_ventana.c) estan en yo2win.h.
 *
 * Sin CRT (ni msvcrt ni ucrt): solo kernel32, user32 y gdi32 (user32/gdi32 solo para la ventana ampliada; yo2.exe ya los
 * carga). Se compila con build_dll.ps1 (MinGW-w64 i686 gcc/clang, zig cc o MSVC x86).
 * La cadena MARCA identifica la DLL como nuestra: el lanzador no pisa una version.dll que no la lleve.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include "yo2win.h"

#define MARCA "YO2ONLINE-NETDLL 2"
/* El lanzador busca MARCA dentro del fichero. Con -O2 el compilador puede partir la literal en
 * instrucciones y la cadena deja de estar seguida: este array se queda en el binario sí o sí. */
static const char marca_fichero[] __attribute__((used)) = MARCA;

/* Sin CRT no hay ___chkstk: ninguna funcion puede tener mas de 4 KB de pila. Las que llevan una Linea (1 KB) no se
 * dejan inlinear, para que el compilador no junte varias en un mismo marco. */
#if defined(_MSC_VER)
#define NOINL __declspec(noinline)
#else
#define NOINL __attribute__((noinline))
#endif

/* ------------------------------------------------------------------ sin CRT */
/* El compilador puede generar llamadas a memset/memcpy (inicializar o copiar arrays). volatile evita que gcc convierta
 * el bucle en una llamada a si mismo. */
#if defined(_MSC_VER)
#pragma function(memset, memcpy)
#endif
void *memset(void *d, int c, size_t n) {
    volatile unsigned char *p = (volatile unsigned char *)d;
    while (n--) *p++ = (unsigned char)c;
    return d;
}
void *memcpy(void *d, const void *s, size_t n) {
    volatile unsigned char *p = (volatile unsigned char *)d;
    const unsigned char *q = (const unsigned char *)s;
    while (n--) *p++ = *q++;
    return d;
}

static int largo(const char *s) { int n = 0; while (s[n]) n++; return n; }
static char minus(char c) { return (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c; }
static WCHAR minusw(WCHAR c) { return (c >= L'A' && c <= L'Z') ? (WCHAR)(c + 32) : c; }
static int igual(const char *a, const char *b) { while (*a && *a == *b) { a++; b++; } return *a == *b; }
static int igual_ci(const char *a, const char *b) { while (*a && minus(*a) == minus(*b)) { a++; b++; } return minus(*a) == minus(*b); }
static int empieza_ci(const char *s, const char *pre) { while (*pre) { if (minus(*s) != minus(*pre)) return 0; s++; pre++; } return 1; }
static int acaba_ci(const char *s, const char *suf) {
    int a = largo(s), b = largo(suf);
    return a >= b && igual_ci(s + a - b, suf);
}

/* ------------------------------------------------------------------ registro (YO2ONLINE_NET_LOG) */
static HANDLE g_log = INVALID_HANDLE_VALUE;

typedef struct { char b[1024]; int n; } Linea;
static void l_s(Linea *l, const char *s) { while (s && *s && l->n < (int)sizeof(l->b) - 3) l->b[l->n++] = *s++; }
static void l_u(Linea *l, unsigned long v) {
    char t[12]; int k = 0;
    do { t[k++] = (char)('0' + v % 10); v /= 10; } while (v && k < 11);
    while (k && l->n < (int)sizeof(l->b) - 3) l->b[l->n++] = t[--k];
}
static void l_i(Linea *l, long v) { if (v < 0) { l_s(l, "-"); l_u(l, (unsigned long)(-v)); } else l_u(l, (unsigned long)v); }
static void l_x(Linea *l, unsigned long v) {
    static const char hx[] = "0123456789abcdef";
    int i; l_s(l, "0x");
    for (i = 28; i >= 0; i -= 4) if (l->n < (int)sizeof(l->b) - 3) l->b[l->n++] = hx[(v >> i) & 15];
}
static void l_2(Linea *l, unsigned v) {
    if (v < 10) l_s(l, "0");
    l_u(l, v);
}
static void l_ini(Linea *l) {
    SYSTEMTIME st;
    GetLocalTime(&st);
    l->n = 0;
    l_2(l, st.wHour); l_s(l, ":"); l_2(l, st.wMinute); l_s(l, ":"); l_2(l, st.wSecond);
    l_s(l, " [yo2net] ");
}
static void l_fin(Linea *l) {
    DWORD w;
    if (g_log == INVALID_HANDLE_VALUE) return;
    l->b[l->n++] = '\r'; l->b[l->n++] = '\n';
    WriteFile(g_log, l->b, (DWORD)l->n, &w, NULL);
}
static NOINL void log1(const char *a, const char *b) { Linea l; l_ini(&l); l_s(&l, a); l_s(&l, b); l_fin(&l); }

static NOINL void abrir_log(void) {
    WCHAR p[1024];
    DWORD n = GetEnvironmentVariableW(L"YO2ONLINE_NET_LOG", p, 1024);
    if (n == 0 || n >= 1024) return;
    g_log = CreateFileW(p, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_ALWAYS,
                        FILE_ATTRIBUTE_NORMAL, NULL);
}

/* ------------------------------------------------------------------ reenvio a la version.dll de sistema */
#define N_EXPORTS 17
static const char *const NOMBRES[N_EXPORTS] = {
    "GetFileVersionInfoA", "GetFileVersionInfoByHandle", "GetFileVersionInfoExA", "GetFileVersionInfoExW",
    "GetFileVersionInfoSizeA", "GetFileVersionInfoSizeExA", "GetFileVersionInfoSizeExW", "GetFileVersionInfoSizeW",
    "GetFileVersionInfoW", "VerFindFileA", "VerFindFileW", "VerInstallFileA", "VerInstallFileW", "VerLanguageNameA",
    "VerLanguageNameW", "VerQueryValueA", "VerQueryValueW",
};
/* Global (no static): los stubs de abajo saltan por aqui con "jmp [g_real+4*i]". Un hueco a NULL (exportacion que no
 * existe en un Windows muy viejo, p. ej. las Ex en XP) solo falla si alguien la llama, igual que sin proxy. */
#if defined(__GNUC__)
__attribute__((used))
#endif
void *g_real[N_EXPORTS];

/* Cada exportacion es un salto indirecto: no hace falta saber cuantos argumentos tiene (stdcall). Los nombres
 * exportados salen de version.def (Nombre = stub_Nombre). */
#define CADENA2(x) #x
#define CADENA(x) CADENA2(x)
#if defined(_MSC_VER)
#define STUB(i, n) __declspec(naked) void stub_##n(void) { __asm jmp dword ptr [g_real + 4 * i] }
#else
/* naked: sin prologo; solo asm basico (lo que gcc admite en naked). El prefijo de simbolo es "_" en i686-mingw. */
#define STUB(i, n) __attribute__((naked)) void stub_##n(void) { __asm__("jmp *" CADENA(__USER_LABEL_PREFIX__) "g_real+4*" #i); }
#endif
STUB(0, GetFileVersionInfoA)
STUB(1, GetFileVersionInfoByHandle)
STUB(2, GetFileVersionInfoExA)
STUB(3, GetFileVersionInfoExW)
STUB(4, GetFileVersionInfoSizeA)
STUB(5, GetFileVersionInfoSizeExA)
STUB(6, GetFileVersionInfoSizeExW)
STUB(7, GetFileVersionInfoSizeW)
STUB(8, GetFileVersionInfoW)
STUB(9, VerFindFileA)
STUB(10, VerFindFileW)
STUB(11, VerInstallFileA)
STUB(12, VerInstallFileW)
STUB(13, VerLanguageNameA)
STUB(14, VerLanguageNameW)
STUB(15, VerQueryValueA)
STUB(16, VerQueryValueW)

static NOINL int cargar_real(void) {
    static const char suf[] = "\\version.dll";
    char ruta[MAX_PATH + 16];
    HMODULE m;
    int i;
    /* En un proceso de 32 bits en Windows de 64, "system32" lo redirige el sistema a SysWOW64. Ruta completa: con el
     * nombre a secas el cargador devolveria esta misma DLL (ya cargada como version.dll). */
    UINT n = GetSystemDirectoryA(ruta, MAX_PATH);
    if (n == 0 || n >= MAX_PATH) return 0;
    for (i = 0; suf[i]; i++) ruta[n + i] = suf[i];
    ruta[n + i] = 0;
    m = LoadLibraryA(ruta);
    if (!m) return 0;
    for (i = 0; i < N_EXPORTS; i++) g_real[i] = (void *)GetProcAddress(m, NOMBRES[i]);
    return 1;
}

/* ------------------------------------------------------------------ configuracion */
static const char LOCAL[] = "127.0.0.1";
static unsigned g_offset;       /* YO2ONLINE_OFFSET (0 = jugar de verdad) */
static unsigned g_http = 8730;  /* puerto HTTP local (8730 + offset) */

static unsigned leer_uint_env(const char *var) {
    char v[16]; unsigned r = 0; DWORD n = GetEnvironmentVariableA(var, v, sizeof v), i;
    if (n == 0 || n >= sizeof v) return 0;
    for (i = 0; i < n && v[i] >= '0' && v[i] <= '9'; i++) r = r * 10 + (unsigned)(v[i] - '0');
    return r;
}

/* ------------------------------------------------------------------ IAT */
static BYTE *g_exe;
static DWORD g_exe_tam;

/* Hueco de la IAT de yo2.exe para dll!fn (por nombre) o dll!#ord (por ordinal). NULL si no lo importa. */
static void **buscar_iat(const char *dll, const char *fn, WORD ord) {
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)g_exe;
    IMAGE_NT_HEADERS32 *nt;
    IMAGE_DATA_DIRECTORY *dd;
    IMAGE_IMPORT_DESCRIPTOR *d;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return NULL;
    nt = (IMAGE_NT_HEADERS32 *)(g_exe + dos->e_lfanew);
    if (nt->Signature != IMAGE_NT_SIGNATURE) return NULL;
    dd = &nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dd->VirtualAddress) return NULL;
    for (d = (IMAGE_IMPORT_DESCRIPTOR *)(g_exe + dd->VirtualAddress); d->Name; d++) {
        IMAGE_THUNK_DATA32 *nom, *dir;
        if (!igual_ci((const char *)(g_exe + d->Name), dll) || !d->OriginalFirstThunk) continue;
        nom = (IMAGE_THUNK_DATA32 *)(g_exe + d->OriginalFirstThunk);
        dir = (IMAGE_THUNK_DATA32 *)(g_exe + d->FirstThunk);
        for (; nom->u1.AddressOfData; nom++, dir++) {
            DWORD v = nom->u1.Ordinal;
            if (IMAGE_SNAP_BY_ORDINAL32(v)) {
                if (ord && IMAGE_ORDINAL32(v) == ord) return (void **)&dir->u1.Function;
            } else if (fn && igual((const char *)((IMAGE_IMPORT_BY_NAME *)(g_exe + v))->Name, fn)) {
                return (void **)&dir->u1.Function;
            }
        }
    }
    return NULL;
}

/* Pone gancho en el hueco y devuelve la funcion original (NULL si no se pudo). */
static NOINL void *enganchar(const char *nombre, const char *dll, const char *fn, WORD ord, void *gancho) {
    void **h = buscar_iat(dll, fn, ord);
    void *orig;
    DWORD viejo;
    Linea l;
    if (!h) return NULL;
    orig = *h;
    /* Tiene que estar ya resuelta (apunta fuera de yo2.exe): el cargador rellena la IAT del exe antes de llamar al
     * DllMain de sus DLL. Si aun tuviera el RVA del nombre, no se toca. */
    if ((BYTE *)orig >= g_exe && (BYTE *)orig < g_exe + g_exe_tam) {
        log1("gancho sin resolver todavia: ", nombre);
        return NULL;
    }
    if (!VirtualProtect(h, sizeof(void *), PAGE_READWRITE, &viejo)) { log1("VirtualProtect fallo en la IAT: ", nombre); return NULL; }
    *h = gancho;
    VirtualProtect(h, sizeof(void *), viejo, &viejo);
    l_ini(&l); l_s(&l, "gancho "); l_s(&l, nombre); l_s(&l, ": "); l_s(&l, dll); l_s(&l, " IAT ");
    l_x(&l, (unsigned long)(UINT_PTR)h); l_fin(&l);
    return orig;
}

/* ------------------------------------------------------------------ ganchos de red */
typedef void *(WINAPI *gethostbyname_t)(const char *);
typedef int(WINAPI *connect_t)(UINT_PTR, const void *, int);
typedef void *(WINAPI *openurl_t)(void *, const char *, const char *, DWORD, DWORD, DWORD_PTR);
static gethostbyname_t g_gethostbyname;
static connect_t g_connect;
static openurl_t g_openurl;

static void *WINAPI h_gethostbyname(const char *nombre) {
    if (nombre && acaba_ci(nombre, "yugioh-online.net")) {
        Linea l; l_ini(&l); l_s(&l, "dns "); l_s(&l, nombre); l_s(&l, " -> "); l_s(&l, LOCAL); l_fin(&l);
        return g_gethostbyname(LOCAL);
    }
    return g_gethostbyname(nombre);
}

static int WINAPI h_connect(UINT_PTR s, const void *sa, int len) {
    BYTE c[64];
    BYTE *p;
    unsigned puerto;
    int i;
    Linea l;
    if (!sa || len < 8 || *(const WORD *)sa != 2) return g_connect(s, sa, len); /* solo AF_INET */
    if (len <= (int)sizeof c) {          /* se cambia una copia, no la del juego */
        for (i = 0; i < len; i++) c[i] = ((const BYTE *)sa)[i];
        p = c;
    } else {
        p = (BYTE *)sa;                  /* sockaddr raro y grande: en su sitio, como hacia frida */
    }
    puerto = ((unsigned)p[2] << 8) | p[3];
    if (g_offset && puerto == 5730) {
        puerto += g_offset;
        p[2] = (BYTE)(puerto >> 8); p[3] = (BYTE)puerto;
    }
    p[4] = 127; p[5] = 0; p[6] = 0; p[7] = 1;
    l_ini(&l); l_s(&l, "connect -> "); l_s(&l, LOCAL); l_s(&l, ":"); l_u(&l, puerto); l_fin(&l);
    return g_connect(s, p, len);
}

static void *WINAPI h_openurl(void *hi, const char *url, const char *cab, DWORD lcab, DWORD flags, DWORD_PTR ctx) {
    if (url && empieza_ci(url, "http://") && url[7] && url[7] != '/') {
        const char *resto = url + 7;
        char *nu;
        int n;
        while (*resto && *resto != '/') resto++;
        if (largo(resto) < 900) {  /* la linea del registro es de 1024: nada de URL recortadas */
            Linea l;
            l.n = 0;
            l_s(&l, "http://"); l_s(&l, LOCAL); l_s(&l, ":"); l_u(&l, g_http); l_s(&l, resto);
            /* No se libera nunca (como keep[] de redirect_hook.js): WinINet puede seguir usandola. Son pocas URL. */
            nu = (char *)HeapAlloc(GetProcessHeap(), 0, (SIZE_T)l.n + 1);
            if (nu) {
                for (n = 0; n < l.n; n++) nu[n] = l.b[n];
                nu[n] = 0;
                l_ini(&l); l_s(&l, "url "); l_s(&l, url); l_s(&l, " -> "); l_s(&l, nu); l_fin(&l);
                url = nu;
            }
        } else {
            log1("url demasiado larga, sin cambiar: ", url);
        }
    }
    return g_openurl(hi, url, cab, lcab, flags, ctx);
}

/* ------------------------------------------------------------------ aviso 5092 (copia de aviso_sala_hook.js) */
/* Direcciones del exe 080912 (detalle en aviso_sala_hook.js y research/lobby/NOTES.md). */
#define A(x) ((BYTE *)(UINT_PTR)(x))
#define AV_EN          A(0x00a00c21)  /* mov esi,eax / push 0 / mov ecx,esi: eax = barra de menus, esi = escena */
#define AV_VUELTA      A(0x00a00c27)
#define TD_CTASK       A(0x00c83238)
#define TD_ELEMENT     A(0x00c83190)
#define TD_OK          A(0x00c87b08)
#define TD_YESNO       A(0x00c878c8)
#define CB_VTABLE      A(0x00bee8a0)
#define CB_METODO      A(0x0040fa88)
#define WSTR_VACIA     A(0x00be610c)
#define P_RED          A(0x012711ac)
#define P_UIMODE       A(0x012bc618)
#define P_TEXTMNG      A(0x01271840)
#define TEXTO_MOVE     5092

/* Convenciones: __RTDynamicCast y FUN_005e79f0 son cdecl. Los metodos (thiscall: this en ecx, el resto en la pila y los
 * saca el llamado) se llaman como __fastcall con un segundo argumento de relleno (va en edx y se ignora): mismo efecto
 * y lo aceptan MSVC, gcc y clang. */
typedef void *(__cdecl *dyn_t)(void *, int, void *, void *, int);
typedef void *(__cdecl *crear_ok_t)(void *, DWORD, DWORD, DWORD, DWORD);
typedef void(__fastcall *th_ptr_t)(void *, void *, void *);
typedef void(__fastcall *th_int_t)(void *, void *, int);
typedef void *(__fastcall *lee_texto_t)(void *, void *, void *, DWORD);
#define DYN          ((dyn_t)(UINT_PTR)0x00b0b20e)
#define CREAR_OK     ((crear_ok_t)(UINT_PTR)0x005e79f0)
#define ENLAZAR      ((th_ptr_t)(UINT_PTR)0x00628fa0)
#define MODAL        ((th_int_t)(UINT_PTR)0x005df6d0)
#define PON_CB       ((th_ptr_t)(UINT_PTR)0x005df480)
#define BORRAR_WSTR  ((th_int_t)(UINT_PTR)0x00530120)

static int g_env_si, g_env_no;
static WCHAR g_fichero_aviso[MAX_PATH + 32];
static int g_hay_fichero_aviso;

static int legible(const void *p, SIZE_T n) {
    MEMORY_BASIC_INFORMATION mbi;
    const BYTE *a = (const BYTE *)p, *fin = a + n;
    while (a < fin) {
        if (!VirtualQuery(a, &mbi, sizeof mbi) || mbi.State != MEM_COMMIT) return 0;
        if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return 0;
        a = (const BYTE *)mbi.BaseAddress + mbi.RegionSize;
    }
    return 1;
}
static void *leer_ptr(const BYTE *p) { return legible(p, 4) ? *(void *const *)p : NULL; }

static int aviso_activo(void) {
    if (g_env_no) return 0;
    if (g_env_si) return 1;
    return g_hay_fichero_aviso && GetFileAttributesW(g_fichero_aviso) != INVALID_FILE_ATTRIBUTES;
}

/* Lo mismo que hace el cliente para m_pDeckErr; NULL si abre la ventana o el motivo por el que no. */
static NOINL const char *abrir_aviso(BYTE *esc, DWORD texto) {
    static void *cb[3];   /* retrollamada {vtable, escena, metodo}: la ventana guarda un clon */
    void *tmp[8];         /* std::wstring de 0x20 bytes que rellena el gestor de textos */
    BYTE *padre, *dlg, *tp, *tm, *ws;
    void *w;
    int i;
    if (DYN(leer_ptr(esc + 0x154), 0, TD_CTASK, TD_OK, 0)) return "ya hay una ventana OK de la escena";
    if (DYN(leer_ptr(esc + 0x1d8), 0, TD_CTASK, TD_YESNO, 0)) return "hay un Si/No abierto";
    padre = (BYTE *)DYN(leer_ptr(esc + 0x78), 0, TD_CTASK, TD_ELEMENT, 0);
    if (!padre) return "escena sin elemento raiz";
    tm = (BYTE *)leer_ptr(P_TEXTMNG);
    if (!tm || !legible(tm, 4)) return "sin gestor de textos";
    *(int *)(esc + 0xc4) = -1;
    dlg = (BYTE *)CREAR_OK(padre, 0x43d20000, 0x438c0000, 0xfa2, 1); /* x 420.0, y 280.0, boton 4002 */
    if (!dlg) return "FUN_005e79f0 devolvio 0";
    tp = esc + 0x150;                                                /* yo2TaskPtr m_pDeckErr */
    *(void **)(tp + 4) = dlg;
    ENLAZAR(dlg, 0, tp);
    MODAL(dlg, 0, 1);
    cb[0] = CB_VTABLE; cb[1] = esc; cb[2] = CB_METODO;
    PON_CB(dlg, 0, cb);
    for (i = 0; i < 8; i++) tmp[i] = 0;
    ws = (BYTE *)((lee_texto_t)(*(void ***)tm)[1])(tm, 0, tmp, texto);
    w = ws ? *(void **)(ws + 4) : NULL;
    if (!w) w = WSTR_VACIA;
    ((th_ptr_t)(*(void ***)dlg)[0x30 / 4])(dlg, 0, w);
    BORRAR_WSTR(tmp, 0, 1);
    return NULL;
}

static NOINL void log_aviso(const char *ev, const char *aviso, const char *motivo, long extra) {
    Linea l;
    l_ini(&l); l_s(&l, "[aviso_sala] "); l_s(&l, ev); l_s(&l, " hueco 2");
    if (aviso) { l_s(&l, " aviso "); l_s(&l, aviso); }
    if (motivo) { l_s(&l, " motivo: "); l_s(&l, motivo); }
    if (extra >= 0) { l_s(&l, " "); l_i(&l, extra); }
    l_fin(&l);
}

/* Lo llama el trampolin con los registros de 0xa00c21 (cdecl). Solo mira el hueco 2 (MOVE): MAIN no tiene texto. */
static void __cdecl aviso_en_clic(BYTE *menu, BYTE *esc) {
    BYTE *red, *t;
    const char *no;
    int modo;
    if (!menu || !esc || !legible(menu + 0x3a4, 0x3cc + 3 - 0x3a4)) return;
    if (menu[0x3cc + 2] == 0 || *(void **)(menu + 0x3a4 + 4 * 2) == NULL) return;
    red = (BYTE *)leer_ptr(P_RED);
    if (!red) return;
    t = (BYTE *)leer_ptr(red + 0xc6c);
    if (!t || !legible(t + 0xd8b, 1) || t[0xd8b] == 0) return; /* bloqueo por otra cosa: no es lo nuestro */
    if (!aviso_activo()) { log_aviso("clic_bloqueado", "apagado", NULL, -1); return; }
    modo = legible(P_UIMODE, 4) ? *(int *)P_UIMODE : -1;
    if (modo != 0) { log_aviso("clic_bloqueado", "no", "modo de interfaz", modo); return; }
    if (!legible(esc, 0x1dc)) return;
    no = abrir_aviso(esc, TEXTO_MOVE);
    if (no) log_aviso("clic_bloqueado", "no", no, -1);
    else log_aviso("aviso_abierto", NULL, NULL, TEXTO_MOVE);
}

static void pon32(BYTE *p, DWORD v) { p[0] = (BYTE)v; p[1] = (BYTE)(v >> 8); p[2] = (BYTE)(v >> 16); p[3] = (BYTE)(v >> 24); }

static NOINL void aviso_instalar(void) {
    static const BYTE esperado[6] = {0x8b, 0xf0, 0x6a, 0x00, 0x8b, 0xce};
    char v[16];
    DWORD n, viejo;
    BYTE *t;
    int i, k = 0;
    Linea l;

    n = GetEnvironmentVariableA("YO2_AVISO_SALA", v, sizeof v);
    if (n > 0 && n < sizeof v) {
        char s[16]; int a = 0, b = (int)n;
        while (a < b && (v[a] == ' ' || v[a] == '\t')) a++;
        while (b > a && (v[b - 1] == ' ' || v[b - 1] == '\t' || v[b - 1] == '\r' || v[b - 1] == '\n')) b--;
        for (i = 0; a + i < b; i++) s[i] = minus(v[a + i]);
        s[i] = 0;
        g_env_si = igual(s, "1") || igual(s, "si") || igual(s, "on") || igual(s, "true") || igual(s, "yes");
        g_env_no = igual(s, "0") || igual(s, "no") || igual(s, "off") || igual(s, "false");
    }
    n = GetModuleFileNameW(NULL, g_fichero_aviso, MAX_PATH);
    if (n > 0 && n < MAX_PATH) {
        static const WCHAR f[] = L"yo2-aviso-sala.txt";
        while (n > 0 && g_fichero_aviso[n - 1] != L'\\' && g_fichero_aviso[n - 1] != L'/') n--;
        for (i = 0; f[i]; i++) g_fichero_aviso[n + i] = f[i];
        g_fichero_aviso[n + i] = 0;
        g_hay_fichero_aviso = 1;
    }

    if (!legible(AV_EN, 6)) { log1("[aviso_sala] error: ", "0xa00c21 no es legible; no se engancha"); return; }
    for (i = 0; i < 6; i++) {
        if (AV_EN[i] != esperado[i]) {
            int j;
            l_ini(&l); l_s(&l, "[aviso_sala] error: bytes inesperados en 0xa00c21:");
            for (j = 0; j < 6; j++) { l_s(&l, " "); l_x(&l, AV_EN[j]); }
            l_s(&l, "; no se engancha"); l_fin(&l);
            return;
        }
    }
    /* Trampolin: guarda todo, llama aviso_en_clic(eax, esi), restaura, hace las 6 instrucciones que se pisan y vuelve. */
    t = (BYTE *)VirtualAlloc(NULL, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
    if (!t) { log1("[aviso_sala] error: ", "VirtualAlloc"); return; }
    t[k++] = 0x60;                                   /* pushad */
    t[k++] = 0x9c;                                   /* pushfd */
    t[k++] = 0x56;                                   /* push esi  (escena) */
    t[k++] = 0x50;                                   /* push eax  (barra de menus) */
    t[k++] = 0xe8; pon32(t + k, (DWORD)((UINT_PTR)aviso_en_clic - (UINT_PTR)(t + k + 4))); k += 4; /* call */
    t[k++] = 0x83; t[k++] = 0xc4; t[k++] = 0x08;     /* add esp,8 */
    t[k++] = 0x9d;                                   /* popfd */
    t[k++] = 0x61;                                   /* popad */
    for (i = 0; i < 6; i++) t[k++] = esperado[i];    /* mov esi,eax / push 0 / mov ecx,esi */
    t[k++] = 0xe9; pon32(t + k, (DWORD)((UINT_PTR)AV_VUELTA - (UINT_PTR)(t + k + 4))); k += 4;     /* jmp 0xa00c27 */
    VirtualProtect(t, 64, PAGE_EXECUTE_READ, &viejo);
    FlushInstructionCache(GetCurrentProcess(), t, 64);

    if (!VirtualProtect(AV_EN, 6, PAGE_EXECUTE_READWRITE, &viejo)) { log1("[aviso_sala] error: ", "VirtualProtect"); return; }
    AV_EN[0] = 0xe9; pon32(AV_EN + 1, (DWORD)((UINT_PTR)t - (UINT_PTR)(AV_EN + 5))); AV_EN[5] = 0x90;
    VirtualProtect(AV_EN, 6, viejo, &viejo);
    FlushInstructionCache(GetCurrentProcess(), AV_EN, 6);
    l_ini(&l); l_s(&l, "[aviso_sala] cargado en 0xa00c21, activo ahora: "); l_s(&l, aviso_activo() ? "si" : "no");
    l_fin(&l);
}

/* ------------------------------------------------------------------ ventana ampliada (YO2ONLINE_WINDOW, rx/PAN 24-10) */
/*
 * Por que hace falta (ingenieria inversa de yo2.exe 080912, volcado v2):
 *   - El juego lee el raton SOLO con GetCursorPos (CMouse::GetPos 0x542080, CMouseLayer 0x542eb0) y con el lParam de
 *     WM_MOUSEMOVE/WM_*BUTTON* en 0x5e2700. No usa DirectInput, ni ClipCursor, ni SetCapture.
 *   - Con DAT_0126d420 = 0 («Window» en las opciones) CMouse::GetPos hace (cursor - ClientToScreen) * 1024.0 / ancho_cliente
 *     (constantes 0xbe963c = 1024.0 y 0xbe9640 = 768.0): escala bien. 0x5e2700 hace lo mismo con el lParam. CMouseLayer NO
 *     escala (cursor - origen cliente, a pelo): con una ventana mayor que 1024x768 se desfasa.
 *   - Con DAT_0126d420 = 1 («Full screen»), GetPos y CMouseLayer devuelven GetCursorPos TAL CUAL: dan por hecho que el
 *     escritorio mide 1024x768 (Direct3D cambia el modo de pantalla). DXVK no lo cambia: estira el 1024x768 al monitor y el
 *     juego recibe coordenadas de pantalla (p. ej. 0..1919) como si fueran suyas -> los clics salen descuadrados, mas
 *     cuanto mas lejos de la esquina superior izquierda.
 * Solucion: el juego sigue creyendo que todo mide 1024x768 y la DLL traduce en las puertas de entrada:
 *   GetCursorPos / SetCursorPos / ClientToScreen / ScreenToClient / GetClientRect (solo de SU ventana) y el lParam de los
 *   mensajes de raton (PeekMessageA/GetMessageA). El area cliente es "virtual": origen (0,0) y 1024x768; GetCursorPos
 *   devuelve ya coordenadas del juego. Asi todos los caminos (ventana, pantalla completa, 0x5e2700, CMouseLayer) cuadran
 *   sin tocar el codigo del juego. Direct3D se queda en ventana (CreateDevice/Reset con Windowed=1: el backbuffer sigue
 *   siendo 1024x768 y lo estira al presentar), la ventana real se crea del tamano del modo y, en «fit», sin bordes a
 *   pantalla completa del monitor con un fondo negro detras para las bandas 4:3.
 */
static int g_vmodo;            /* VW_*; VW_ORIG = apagado (no se engancha nada de esto) */
static HWND g_hwnd;            /* la ventana principal del juego: la primera grande que crea */
static int g_vtraza;           /* YO2ONLINE_WINDOW_TRACE=1: apunta lo que lee el juego del raton (solo pruebas) */
static int g_vtraza_n;
static HMODULE g_dll;

typedef HWND(WINAPI *createwindow_t)(DWORD, LPCSTR, LPCSTR, DWORD, int, int, int, int, HWND, HMENU, HINSTANCE, LPVOID);
typedef BOOL(WINAPI *setwindowpos_t)(HWND, HWND, int, int, int, int, UINT);
typedef LONG(WINAPI *setwindowlong_t)(HWND, int, LONG);
typedef BOOL(WINAPI *getcursorpos_t)(POINT *);
typedef BOOL(WINAPI *setcursorpos_t)(int, int);
typedef BOOL(WINAPI *c2s_t)(HWND, POINT *);
typedef BOOL(WINAPI *getclientrect_t)(HWND, RECT *);
typedef BOOL(WINAPI *peekmessage_t)(MSG *, HWND, UINT, UINT, UINT);
typedef BOOL(WINAPI *getmessage_t)(MSG *, HWND, UINT, UINT);
typedef void *(WINAPI *d3dcreate8_t)(UINT);
typedef HRESULT(WINAPI *createdevice_t)(void *, UINT, UINT, HWND, DWORD, DWORD *, void **);
typedef HRESULT(WINAPI *reset_t)(void *, DWORD *);
static createwindow_t g_o_createwindow;
static setwindowpos_t g_o_setwindowpos;
static setwindowlong_t g_o_setwindowlong;
static getcursorpos_t g_o_getcursorpos;
static setcursorpos_t g_o_setcursorpos;
static c2s_t g_o_clienttoscreen, g_o_screentoclient;
static getclientrect_t g_o_getclientrect;
static peekmessage_t g_o_peekmessage;
static getmessage_t g_o_getmessage;
static d3dcreate8_t g_o_d3dcreate8;
static createdevice_t g_o_createdevice;
static reset_t g_o_reset;

/* Origen y tamano REALES del area cliente de la ventana del juego (siempre por las funciones de verdad: no pasa por
 * nuestros ganchos, que solo estan en la tabla de importaciones de yo2.exe). 0 si no hay ventana util (minimizada...). */
static int cliente_real(POINT *o, int *cw, int *ch) {
    RECT c;
    if (!g_hwnd || !IsWindow(g_hwnd)) return 0;
    o->x = 0; o->y = 0;
    if (!ClientToScreen(g_hwnd, o) || !GetClientRect(g_hwnd, &c)) return 0;
    *cw = c.right - c.left; *ch = c.bottom - c.top;
    return *cw > 0 && *ch > 0;
}

static NOINL void traza_raton(const char *que, int x, int y, int rx, int ry) {
    Linea l;
    static int ux = -99999, uy = -99999;
    if (!g_vtraza || g_vtraza_n > 400) return;
    if (rx == ux && ry == uy) return;                   /* el juego pregunta muchas veces lo mismo */
    ux = rx; uy = ry;
    g_vtraza_n++;
    l_ini(&l); l_s(&l, "[ventana] "); l_s(&l, que); l_s(&l, " real "); l_i(&l, rx); l_s(&l, ","); l_i(&l, ry);
    l_s(&l, " -> juego "); l_i(&l, x); l_s(&l, ","); l_i(&l, y); l_fin(&l);
}

static BOOL WINAPI h_getcursorpos(POINT *pt) {
    POINT o; int cw, ch, rx, ry;
    BOOL r = g_o_getcursorpos ? g_o_getcursorpos(pt) : GetCursorPos(pt);
    if (r && pt && cliente_real(&o, &cw, &ch)) {
        rx = pt->x; ry = pt->y;
        pt->x = vw_a_juego(rx, o.x, cw, VW_BASE_W);
        pt->y = vw_a_juego(ry, o.y, ch, VW_BASE_H);
        traza_raton("GetCursorPos", pt->x, pt->y, rx, ry);
    }
    return r;
}

static BOOL WINAPI h_setcursorpos(int x, int y) {
    POINT o; int cw, ch;
    if (cliente_real(&o, &cw, &ch)) {
        int rx = vw_a_real(x, o.x, cw, VW_BASE_W), ry = vw_a_real(y, o.y, ch, VW_BASE_H);
        traza_raton("SetCursorPos", x, y, rx, ry);
        x = rx; y = ry;
    }
    return g_o_setcursorpos ? g_o_setcursorpos(x, y) : SetCursorPos(x, y);
}

/* Para el juego el area cliente empieza en (0,0) de "su pantalla": ClientToScreen/ScreenToClient no suman nada. */
static BOOL WINAPI h_clienttoscreen(HWND h, POINT *pt) {
    if (g_hwnd && h == g_hwnd) return TRUE;
    return g_o_clienttoscreen(h, pt);
}
static BOOL WINAPI h_screentoclient(HWND h, POINT *pt) {
    if (g_hwnd && h == g_hwnd) return TRUE;
    return g_o_screentoclient(h, pt);
}
static BOOL WINAPI h_getclientrect(HWND h, RECT *rc) {
    BOOL r = g_o_getclientrect(h, rc);
    if (r && rc && g_hwnd && h == g_hwnd) { rc->right = rc->left + VW_BASE_W; rc->bottom = rc->top + VW_BASE_H; }
    return r;
}

/* lParam de los mensajes de raton: de la ventana real a 1024x768. WM_MOUSEWHEEL (0x20a) trae coordenadas de PANTALLA. */
static void mensaje_a_juego(MSG *m) {
    POINT o; int cw, ch, x, y;
    if (!m || !g_hwnd || m->hwnd != g_hwnd) return;
    if (!((m->message >= 0x200 && m->message <= 0x20d))) return;
    if (!cliente_real(&o, &cw, &ch)) return;
    x = (short)LOWORD(m->lParam); y = (short)HIWORD(m->lParam);
    if (m->message == 0x20a) {
        x = vw_a_juego(x, o.x, cw, VW_BASE_W); y = vw_a_juego(y, o.y, ch, VW_BASE_H);
    } else {
        x = vw_a_juego(x, 0, cw, VW_BASE_W); y = vw_a_juego(y, 0, ch, VW_BASE_H);
    }
    if (x < -32768) x = -32768;
    if (x > 32767) x = 32767;
    if (y < -32768) y = -32768;
    if (y > 32767) y = 32767;
    m->lParam = (LPARAM)(DWORD)(((WORD)(short)x) | ((DWORD)(WORD)(short)y << 16));
}
static BOOL WINAPI h_peekmessage(MSG *m, HWND h, UINT a, UINT b, UINT rem) {
    BOOL r = g_o_peekmessage(m, h, a, b, rem);
    if (r && (rem & PM_REMOVE)) mensaje_a_juego(m);     /* sin PM_REMOVE el mensaje se queda en la cola: se vera de nuevo */
    return r;
}
static BOOL WINAPI h_getmessage(MSG *m, HWND h, UINT a, UINT b) {
    BOOL r = g_o_getmessage(m, h, a, b);
    if (r > 0) mensaje_a_juego(m);
    return r;
}

/* ---- geometria de la ventana real */
static void monitor_de(HWND w, RECT *mon, RECT *work) {
    MONITORINFO mi;
    HMONITOR hm;
    POINT pt;
    if (w) hm = MonitorFromWindow(w, MONITOR_DEFAULTTONEAREST);
    else { GetCursorPos(&pt); hm = MonitorFromPoint(pt, MONITOR_DEFAULTTOPRIMARY); }
    mi.cbSize = sizeof mi;
    if (!hm || !GetMonitorInfoA(hm, &mi)) {
        mi.rcMonitor.left = mi.rcMonitor.top = 0;
        mi.rcMonitor.right = GetSystemMetrics(SM_CXSCREEN); mi.rcMonitor.bottom = GetSystemMetrics(SM_CYSCREEN);
        mi.rcWork = mi.rcMonitor;
    }
    *mon = mi.rcMonitor; *work = mi.rcWork;
}

/* Rectangulo de VENTANA (con bordes) que quiere el modo, para el estilo dado, en el monitor de w (o el del cursor). */
static NOINL void rect_objetivo(HWND w, DWORD style, DWORD exstyle, RECT *t) {
    RECT mon, work, fr;
    int x, y, cw, ch, bw, bh, ww, wh;
    monitor_de(w, &mon, &work);
    if (g_vmodo == VW_FIT) {
        vw_ajustar(mon.right - mon.left, mon.bottom - mon.top, &x, &y, &cw, &ch);
        t->left = mon.left + x; t->top = mon.top + y; t->right = t->left + cw; t->bottom = t->top + ch;
        return;
    }
    fr.left = fr.top = fr.right = fr.bottom = 0;
    AdjustWindowRectEx(&fr, style, FALSE, exstyle);     /* solo para saber cuanto ocupan los bordes y el titulo */
    bw = fr.right - fr.left; bh = fr.bottom - fr.top;
    vw_cliente(g_vmodo, (work.right - work.left) - bw, (work.bottom - work.top) - bh, &cw, &ch);
    ww = cw + bw; wh = ch + bh;
    t->left = work.left + ((work.right - work.left) - ww) / 2;
    t->top = work.top + ((work.bottom - work.top) - wh) / 2;
    if (t->left < work.left) t->left = work.left;
    if (t->top < work.top) t->top = work.top;
    t->right = t->left + ww; t->bottom = t->top + wh;
}

#define ESTILO_VENTANA (WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX)

/* ---- fondo negro y vigilante del modo «fit» (un hilo propio con su bucle de mensajes) */
static HWND g_fondo;
static int g_encima;           /* ya estan los dos «siempre encima» (solo mientras el juego es la ventana activa) */

static NOINL void fondo_tick(HWND fondo) {
    RECT mon, work, t, r;
    HWND fg;
    int activo, x, y, w, h;
    if (!g_hwnd || !IsWindow(g_hwnd)) { DestroyWindow(fondo); return; }
    fg = GetForegroundWindow();
    activo = IsWindowVisible(g_hwnd) && !IsIconic(g_hwnd) && (fg == g_hwnd || fg == fondo);
    if (activo) {
        monitor_de(g_hwnd, &mon, &work);
        vw_ajustar(mon.right - mon.left, mon.bottom - mon.top, &x, &y, &w, &h);
        t.left = mon.left + x; t.top = mon.top + y; t.right = t.left + w; t.bottom = t.top + h;
        if (!g_encima || !(GetWindowLongA(g_hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)) {   /* (si alguien le quita lo de «encima», se reordena) */
            /* Primero el fondo y despues el juego, los dos «encima de todo»: asi tapan la barra de tareas y el juego queda
             * delante del fondo. Solo mientras el juego esta activo: con un aviso de Windows o Alt+Tab se sueltan. */
            SetWindowPos(fondo, HWND_TOPMOST, mon.left, mon.top, mon.right - mon.left, mon.bottom - mon.top, SWP_NOACTIVATE | SWP_SHOWWINDOW);
            SetWindowPos(g_hwnd, HWND_TOPMOST, t.left, t.top, w, h, SWP_NOACTIVATE);
            g_encima = 1;
        } else {
            if (GetWindowRect(g_hwnd, &r) && (r.left != t.left || r.top != t.top || r.right != t.right || r.bottom != t.bottom))
                SetWindowPos(g_hwnd, NULL, t.left, t.top, w, h, SWP_NOZORDER | SWP_NOACTIVATE);
            if (GetWindowRect(fondo, &r) && (r.left != mon.left || r.top != mon.top || r.right != mon.right || r.bottom != mon.bottom))
                SetWindowPos(fondo, NULL, mon.left, mon.top, mon.right - mon.left, mon.bottom - mon.top, SWP_NOZORDER | SWP_NOACTIVATE);
        }
    } else if (g_encima) {
        SetWindowPos(g_hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE);
        SetWindowPos(fondo, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_HIDEWINDOW);
        g_encima = 0;
    }
}

static LRESULT CALLBACK fondo_proc(HWND h, UINT m, WPARAM w, LPARAM l) {
    switch (m) {
    case WM_MOUSEACTIVATE: return MA_NOACTIVATE;
    case WM_ERASEBKGND: {
        RECT r;
        GetClientRect(h, &r);
        FillRect((HDC)w, &r, (HBRUSH)GetStockObject(BLACK_BRUSH));
        return 1;
    }
    case WM_SETCURSOR: SetCursor(LoadCursorA(NULL, (LPCSTR)IDC_ARROW)); return TRUE;
    case WM_TIMER: fondo_tick(h); return 0;
    case WM_DESTROY: PostQuitMessage(0); return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

static DWORD WINAPI hilo_fondo(LPVOID p) {
    WNDCLASSA wc;
    MSG m;
    (void)p;
    memset(&wc, 0, sizeof wc);
    wc.lpfnWndProc = fondo_proc;
    wc.hInstance = g_dll;
    wc.lpszClassName = "YO2OnlineFondo";
    wc.hCursor = LoadCursorA(NULL, (LPCSTR)IDC_ARROW);
    wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    if (!RegisterClassA(&wc)) { log1("[ventana] error: ", "RegisterClass del fondo"); return 1; }
    g_fondo = CreateWindowExA(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, wc.lpszClassName, "", WS_POPUP, 0, 0, 1, 1, NULL, NULL, g_dll, NULL);
    if (!g_fondo) { log1("[ventana] error: ", "CreateWindow del fondo"); return 1; }
    SetTimer(g_fondo, 1, 150, NULL);
    while (GetMessageA(&m, NULL, 0, 0) > 0) { TranslateMessage(&m); DispatchMessageA(&m); }
    return 0;
}

/* ---- ganchos de la ventana del juego */
static HWND WINAPI h_createwindow(DWORD ex, LPCSTR cls, LPCSTR name, DWORD style, int x, int y, int w, int h, HWND parent,
                                  HMENU menu, HINSTANCE inst, LPVOID param) {
    int juego = g_vmodo != VW_ORIG && !g_hwnd && !parent && w >= 640 && h >= 480;
    HWND r;
    Linea l;
    if (juego) {
        RECT t;
        l_ini(&l); l_s(&l, "[ventana] CreateWindowEx del juego: estilo "); l_x(&l, style); l_s(&l, " ");
        l_i(&l, w); l_s(&l, "x"); l_i(&l, h); l_s(&l, " en "); l_i(&l, x); l_s(&l, ","); l_i(&l, y);
        if (g_vmodo == VW_FIT) style = WS_POPUP | (style & WS_VISIBLE);
        else if ((style & WS_POPUP) && !(style & WS_CAPTION)) style = ESTILO_VENTANA | (style & WS_VISIBLE); /* queria pantalla completa */
        rect_objetivo(NULL, style, ex, &t);
        x = t.left; y = t.top; w = t.right - t.left; h = t.bottom - t.top;
        l_s(&l, " -> estilo "); l_x(&l, style); l_s(&l, " "); l_i(&l, w); l_s(&l, "x"); l_i(&l, h); l_s(&l, " en ");
        l_i(&l, x); l_s(&l, ","); l_i(&l, y); l_fin(&l);
    }
    r = g_o_createwindow(ex, cls, name, style, x, y, w, h, parent, menu, inst, param);
    if (juego && r) {
        g_hwnd = r;
        if (g_vmodo == VW_FIT) {
            HANDLE th = CreateThread(NULL, 0, hilo_fondo, NULL, 0, NULL);
            if (th) CloseHandle(th); else log1("[ventana] error: ", "CreateThread del fondo");
        }
    }
    return r;
}

static BOOL WINAPI h_setwindowpos(HWND hw, HWND after, int x, int y, int cx, int cy, UINT fl) {
    if (g_hwnd && hw == g_hwnd && g_vmodo != VW_ORIG) {
        RECT t;
        Linea l;
        rect_objetivo(hw, (DWORD)GetWindowLongA(hw, GWL_STYLE), (DWORD)GetWindowLongA(hw, GWL_EXSTYLE), &t);
        if (g_vtraza) {
            l_ini(&l); l_s(&l, "[ventana] SetWindowPos del juego: "); l_i(&l, x); l_s(&l, ","); l_i(&l, y); l_s(&l, " "); l_i(&l, cx);
            l_s(&l, "x"); l_i(&l, cy); l_s(&l, " flags "); l_x(&l, fl); l_s(&l, " -> objetivo "); l_i(&l, t.left); l_s(&l, ",");
            l_i(&l, t.top); l_s(&l, " "); l_i(&l, t.right - t.left); l_s(&l, "x"); l_i(&l, t.bottom - t.top); l_fin(&l);
        }
        if (g_vmodo == VW_FIT) {
            if ((fl & (SWP_NOMOVE | SWP_NOSIZE)) != (SWP_NOMOVE | SWP_NOSIZE)) {   /* solo cambios de orden/marco: se dejan */
                x = t.left; y = t.top; cx = t.right - t.left; cy = t.bottom - t.top;
                fl &= ~(UINT)(SWP_NOMOVE | SWP_NOSIZE);
            }
        } else {
            if (!(fl & SWP_NOSIZE)) { cx = t.right - t.left; cy = t.bottom - t.top; }
            if (!(fl & SWP_NOMOVE)) { x = t.left; y = t.top; }   /* el juego centra con 1024x768: se vuelve a centrar */
        }
    }
    return g_o_setwindowpos(hw, after, x, y, cx, cy, fl);
}

/* El juego cambia el estilo al pasar de ventana a pantalla completa y al reves (0x5825c0): «fit» siempre sin bordes, los
 * modos 1.5x/2x siempre con titulo. */
static LONG WINAPI h_setwindowlong(HWND hw, int idx, LONG v) {
    if (g_hwnd && hw == g_hwnd && idx == GWL_STYLE) {
        if (g_vmodo == VW_FIT) v = (LONG)(WS_POPUP | ((DWORD)v & WS_VISIBLE));
        else if (g_vmodo != VW_ORIG && ((DWORD)v & WS_POPUP) && !((DWORD)v & WS_CAPTION)) v = (LONG)(ESTILO_VENTANA | ((DWORD)v & WS_VISIBLE));
    }
    return g_o_setwindowlong(hw, idx, v);
}

/* ---- Direct3D 8 (DXVK o el de Windows): siempre en ventana. El backbuffer sigue siendo de 1024x768. */
static NOINL void forzar_ventana(DWORD *pp, const char *donde) {
    Linea l;
    if (!pp) return;
    /* D3DPRESENT_PARAMETERS de D3D8, en DWORDs: [0] ancho [1] alto [2] formato [3] numero [4] MSAA [5] swap [6] ventana
     * [7] Windowed [8] auto depth [9] formato depth [10] flags [11] refresco (solo pantalla completa) [12] intervalo. */
    l_ini(&l); l_s(&l, "[ventana] "); l_s(&l, donde); l_s(&l, ": backbuffer "); l_u(&l, pp[0]); l_s(&l, "x"); l_u(&l, pp[1]);
    l_s(&l, pp[7] ? " ya en ventana" : " pantalla completa -> ventana"); l_fin(&l);
    pp[7] = 1; pp[11] = 0; pp[12] = 0;
}
static HRESULT WINAPI h_reset(void *self, DWORD *pp) {
    forzar_ventana(pp, "Reset");
    return g_o_reset(self, pp);
}
static int parchear_vtbl(void **vt, int idx, void *nuevo, void **viejo) {
    DWORD old;
    if (vt[idx] == nuevo) return 0;
    if (!VirtualProtect(&vt[idx], sizeof(void *), PAGE_READWRITE, &old)) return -1;
    *viejo = vt[idx];
    vt[idx] = nuevo;
    VirtualProtect(&vt[idx], sizeof(void *), old, &old);
    return 1;
}
static HRESULT WINAPI h_createdevice(void *self, UINT ad, UINT tipo, HWND hw, DWORD fl, DWORD *pp, void **dev) {
    HRESULT r;
    forzar_ventana(pp, "CreateDevice");
    r = g_o_createdevice(self, ad, tipo, hw, fl, pp, dev);
    if (r >= 0 && dev && *dev) {
        int k = parchear_vtbl(*(void ***)*dev, 14, (void *)h_reset, (void **)&g_o_reset);   /* IDirect3DDevice8::Reset */
        if (k < 0) log1("[ventana] error: ", "no pude enganchar Reset");
    }
    return r;
}
static void *WINAPI h_d3dcreate8(UINT ver) {
    void *p = g_o_d3dcreate8(ver);
    if (p) {
        int k = parchear_vtbl(*(void ***)p, 15, (void *)h_createdevice, (void **)&g_o_createdevice);  /* IDirect3D8::CreateDevice */
        if (k < 0) log1("[ventana] error: ", "no pude enganchar CreateDevice");
    }
    return p;
}

/* Solo pruebas: YO2ONLINE_WINDOW_FORCEFS=1 hace que el juego crea que esta en «Full screen» (DAT_0126d420 = 1) aunque
 * system.bin diga ventana, para probar el camino de pantalla completa sin tocar system.bin: la llamada que lee el ajuste
 * (call [edx+0x30], 3 bytes en 0x608188) pasa a «mov al,1 / nop». */
static NOINL void forzar_pantalla_completa(void) {
    static const BYTE esperado[3] = {0xff, 0x52, 0x30};
    BYTE *p = A(0x00608188);
    DWORD viejo;
    int i;
    if (!legible(p, 8)) return;
    for (i = 0; i < 3; i++) if (p[i] != esperado[i]) { log1("[ventana] FORCEFS: bytes inesperados en 0x608188", ""); return; }
    if (!VirtualProtect(p, 3, PAGE_EXECUTE_READWRITE, &viejo)) return;
    p[0] = 0xb0; p[1] = 0x01; p[2] = 0x90;
    VirtualProtect(p, 3, viejo, &viejo);
    FlushInstructionCache(GetCurrentProcess(), p, 3);
    log1("[ventana] FORCEFS: el juego cree que esta en pantalla completa", "");
}

static NOINL void ventana_instalar(void) {
    char v[8];
    Linea l;
    int ok = 1;
    typedef BOOL(WINAPI * dpiaware_t)(void);
    dpiaware_t dpi;
    HMODULE u32;
    g_dll = GetModuleHandleA(NULL);   /* clase del fondo: basta con un HINSTANCE valido */
    if (GetEnvironmentVariableA("YO2ONLINE_WINDOW_TRACE", v, sizeof v) > 0 && v[0] == '1') g_vtraza = 1;

    /* Sin esto Windows (escalado de pantalla al 125/150 %) hace creer al juego que el monitor es mas pequeno y estira la
     * ventana entera (borroso). Con el proceso «consciente de DPI» las medidas son pixeles reales: «1.5x» son 1536 px. */
    u32 = GetModuleHandleA("user32.dll");
    dpi = u32 ? (dpiaware_t)GetProcAddress(u32, "SetProcessDPIAware") : NULL;
    l_ini(&l); l_s(&l, "[ventana] modo "); l_s(&l, g_vmodo == VW_FIT ? "fit" : g_vmodo == VW_X15 ? "1.5x" : "2x");
    l_s(&l, ", DPI aware: "); l_s(&l, dpi && dpi() ? "si" : "no"); l_fin(&l);

#define ENG(var, dll, fn, gancho) do { var = (void *)enganchar(fn, dll, fn, 0, (void *)gancho); \
        if (!var) { log1("[ventana] FALTA gancho: ", fn); ok = 0; } } while (0)
    ENG(g_o_createwindow, "USER32.dll", "CreateWindowExA", h_createwindow);
    ENG(g_o_setwindowpos, "USER32.dll", "SetWindowPos", h_setwindowpos);
    ENG(g_o_setwindowlong, "USER32.dll", "SetWindowLongA", h_setwindowlong);
    /* YO2ONLINE_WINDOW_NOMOUSE=1 (solo pruebas): sin traduccion de raton, para ver el fallo de siempre (clics descuadrados). */
    if (GetEnvironmentVariableA("YO2ONLINE_WINDOW_NOMOUSE", v, sizeof v) > 0 && v[0] == '1') {
        log1("[ventana] NOMOUSE: sin traduccion de raton (solo para ver el fallo de siempre)", "");
    } else {
        ENG(g_o_getcursorpos, "USER32.dll", "GetCursorPos", h_getcursorpos);
        ENG(g_o_setcursorpos, "USER32.dll", "SetCursorPos", h_setcursorpos);
        ENG(g_o_clienttoscreen, "USER32.dll", "ClientToScreen", h_clienttoscreen);
        ENG(g_o_screentoclient, "USER32.dll", "ScreenToClient", h_screentoclient);
        ENG(g_o_getclientrect, "USER32.dll", "GetClientRect", h_getclientrect);
        ENG(g_o_peekmessage, "USER32.dll", "PeekMessageA", h_peekmessage);
        ENG(g_o_getmessage, "USER32.dll", "GetMessageA", h_getmessage);
    }
    ENG(g_o_d3dcreate8, "d3d8.dll", "Direct3DCreate8", h_d3dcreate8);
#undef ENG
    if (GetEnvironmentVariableA("YO2ONLINE_WINDOW_FORCEFS", v, sizeof v) > 0 && v[0] == '1') forzar_pantalla_completa();
    log1(ok ? "[ventana] ganchos OK" : "[ventana] ganchos: FALLO (la ventana se queda como estaba)", "");
}

/* ------------------------------------------------------------------ arranque */
static NOINL int exe_es_yo2(void) {
    WCHAR p[MAX_PATH + 1];
    static const WCHAR nombre[] = L"yo2.exe";
    DWORD n = GetModuleFileNameW(NULL, p, MAX_PATH), b, i;
    if (n == 0 || n >= MAX_PATH) return 0;
    b = n;
    while (b > 0 && p[b - 1] != L'\\' && p[b - 1] != L'/') b--;
    for (i = 0; nombre[i]; i++) if (b + i >= n || minusw(p[b + i]) != nombre[i]) return 0;
    return b + i == n;
}

static NOINL void iniciar(void) {
    char v[8];
    DWORD n = GetEnvironmentVariableA("YO2ONLINE_NET", v, sizeof v);
    IMAGE_DOS_HEADER *dos;
    int ok = 1;
    int red = !(n == 0 || n >= sizeof v || v[0] == '0');
    Linea l;
    /* Ventana ampliada (rx/PAN): YO2ONLINE_WINDOW = orig | 1.5 | 2 | fit. Vacio, "orig" o desconocido = no se toca nada. */
    n = GetEnvironmentVariableA("YO2ONLINE_WINDOW", v, sizeof v);
    g_vmodo = (n > 0 && n < sizeof v) ? vw_modo_de_texto(v) : VW_ORIG;
    if (g_vmodo < 0) g_vmodo = VW_ORIG;
    if (!red && g_vmodo == VW_ORIG) return;              /* juego abierto sin el lanzador: no se toca nada */
    if (!exe_es_yo2()) return;                           /* otro exe de la carpeta (Yo2Launcher.exe de Konami...) */
    abrir_log();
    g_exe = (BYTE *)GetModuleHandleA(NULL);
    dos = (IMAGE_DOS_HEADER *)g_exe;
    g_exe_tam = ((IMAGE_NT_HEADERS32 *)(g_exe + dos->e_lfanew))->OptionalHeader.SizeOfImage;
    g_offset = leer_uint_env("YO2ONLINE_OFFSET");
    if (g_offset > 50000) g_offset = 0;
    g_http = 8730 + g_offset;
    l_ini(&l); l_s(&l, marca_fichero);
    if (red) {
        l_s(&l, " cargada; destino "); l_s(&l, LOCAL); l_s(&l, ", puerta ");
        l_u(&l, 5730 + g_offset); l_s(&l, ", http "); l_u(&l, g_http);
    } else {
        l_s(&l, " cargada; solo ventana (sin YO2ONLINE_NET)");
    }
    l_fin(&l);

    if (red) {
        g_gethostbyname = (gethostbyname_t)enganchar("gethostbyname", "WSOCK32.dll", "gethostbyname", 52, (void *)h_gethostbyname);
        if (!g_gethostbyname) g_gethostbyname = (gethostbyname_t)enganchar("gethostbyname", "WS2_32.dll", "gethostbyname", 52, (void *)h_gethostbyname);
        g_connect = (connect_t)enganchar("connect", "WSOCK32.dll", "connect", 4, (void *)h_connect);
        if (!g_connect) g_connect = (connect_t)enganchar("connect", "WS2_32.dll", "connect", 4, (void *)h_connect);
        g_openurl = (openurl_t)enganchar("InternetOpenUrlA", "WININET.dll", "InternetOpenUrlA", 0, (void *)h_openurl);
        if (!g_gethostbyname) { log1("FALTA gancho: ", "gethostbyname"); ok = 0; }
        if (!g_connect) { log1("FALTA gancho: ", "connect"); ok = 0; }
        if (!g_openurl) { log1("FALTA gancho: ", "InternetOpenUrlA"); ok = 0; }

        aviso_instalar();  /* opcional (como en frida): si falla se apunta y el juego sigue */
    }

    /* Ventana ampliada: opcional como el aviso (si falla se apunta y el juego sigue como siempre). */
    if (g_vmodo != VW_ORIG) ventana_instalar();

    /* El lanzador busca esta linea: sin ella, la proxima vez vuelve a frida. */
    if (red) log1(ok ? "ganchos OK" : "ganchos: FALLO", "");
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD motivo, LPVOID reservado) {
    (void)reservado;
    if (motivo == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(h);
        if (!cargar_real()) return FALSE; /* sin la version.dll de sistema no hay nada que reenviar */
        iniciar();
    }
    return TRUE;
}
