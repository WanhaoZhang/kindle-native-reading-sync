/* SPDX-License-Identifier: AGPL-3.0-only
 * Passive native-reader collector. No networking, display, wake lock or Wi-Fi
 * changes. Linux production: inotify + poll. --poll-ms exists solely for
 * portable test hosts.
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <sys/inotify.h>
#include <sys/syscall.h>
#endif
#define MAX_CURSORS 256
#define LINE_CAP 65536
#define PATH_CAP 4096
static volatile sig_atomic_t stopping;
struct cursor {
  unsigned long long dev, ino, off;
  char path[PATH_CAP];
  char *part;
  size_t len, capacity;
  int discard;
  unsigned long long gz_size, gz_probe_size;
  long long gz_probed_ms;
  int gz_done;
};
static struct cursor curs[MAX_CURSORS];
static int nc;
static char root[PATH_CAP] = "/var/local/kindle-native-sync",
            logpath[PATH_CAP] = "/var/log/messages";
static char rotated_dir[PATH_CAP] = "/var/local/log";
static char logdir[PATH_CAP], logbase[PATH_CAP], activepath[PATH_CAP],
    sealedpath[PATH_CAP], checkpoint[PATH_CAP], runtimepath[PATH_CAP];
static int active_unsafe = 0;
static int livefd = -1;
static unsigned long long live_dev, live_ino;
static int activefd = -1, controlfd = -1, lockfd = -1, poll_ms = 0,
           notifyfd = -1, dirty = 0, blocked = 0, coverage_gap = 0,
           reading_live = 1;
static long long bootstrap, idle_since, last_sync, active_committed,
    idle_mono_ms;
/* PW2 /var/local is about 62 MiB. Keep 20 MiB for the system plus a
   separate 4 MiB allowance for the worker's bounded SQLite transaction. */
static unsigned long long queue_max = 4 * 1024 * 1024,
                          reserve_min = 24 * 1024 * 1024 + 64 * 1024,
                          queue_cached, free_cached;
static long long room_checked, gzip_due_ms;
static char reader[16] = "unknown", power[16] = "unknown";
static int usb = 0, net = 0, ever_opened = 0, booklet_stopping = 0;
static pid_t worker = 0;
static char last_worker_mode[16] = "";
static unsigned serialno;
static void die(const char *s) {
  perror(s);
  exit(1);
}
static void on_signal(int s) {
  (void)s;
  stopping = 1;
}
static long long nowsec(void) { return (long long)time(NULL); }
static long long monotonic_ms(void) {
  struct timespec t;
  if (clock_gettime(CLOCK_MONOTONIC, &t))
    die("monotonic clock");
  return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static void join(char *out, const char *a, const char *b) {
  if (snprintf(out, PATH_CAP, "%s/%s", a, b) >= PATH_CAP) {
    errno = ENAMETOOLONG;
    die("path");
  }
}
static int mkdir_private(const char *p) {
  if (mkdir(p, 0700) && errno != EEXIST)
    return -1;
  struct stat s;
  if (lstat(p, &s) || !S_ISDIR(s.st_mode) || s.st_uid != geteuid() ||
      (s.st_mode & 0077)) {
    errno = EPERM;
    return -1;
  }
  return 0;
}
static void fsyncdir(const char *p) {
  int fd = open(p, O_RDONLY | O_DIRECTORY);
  if (fd >= 0) {
    fsync(fd);
    close(fd);
  }
}
/* Best-effort private-filesystem guard. Other firmware writers are concurrent;
 * fresh checks are a floor, not an exclusive reservation of filesystem blocks.
 */
static void storage_blocked(void) {
  if (!blocked) {
    blocked = 1;
    if (worker > 0)
      kill(worker, SIGTERM);
  }
}
static int fresh_space(size_t extra, unsigned long long floor,
                       unsigned long long overhead) {
  struct statvfs v;
  if (statvfs(root, &v) || !v.f_bsize || !v.f_frsize) {
    storage_blocked();
    return 0;
  }
  unsigned long long block = (unsigned long long)v.f_bsize;
  unsigned long long fragments = (unsigned long long)v.f_bavail;
  unsigned long long unit = (unsigned long long)v.f_frsize;
  if (fragments > ULLONG_MAX / unit ||
      (unsigned long long)extra > ULLONG_MAX - (block - 1)) {
    errno = EOVERFLOW;
    storage_blocked();
    return 0;
  }
  unsigned long long rounded =
      ((unsigned long long)extra + block - 1) / block * block;
  if (floor > ULLONG_MAX - overhead ||
      rounded > ULLONG_MAX - floor - overhead) {
    errno = EOVERFLOW;
    storage_blocked();
    return 0;
  }
  free_cached = fragments * unit;
  if (free_cached < floor + overhead + rounded) {
    errno = ENOSPC;
    storage_blocked();
    return 0;
  }
  return 1;
}
static int atomic_text(const char *path, const char *text) {
  /* A failure/status record must remain writable below the ordinary 24 MiB
   * capture threshold, but may not spend the system's 20 MiB safety floor. */
  size_t n = strlen(text);
  if (!fresh_space(n, 20ULL * 1024 * 1024, 64ULL * 1024))
    return -1;
  char tmp[PATH_CAP];
  if (snprintf(tmp, sizeof tmp, "%s.tmp-%ld", path, (long)getpid()) >=
      (int)sizeof tmp) {
    errno = ENAMETOOLONG;
    storage_blocked();
    return -1;
  }
  int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
  if (fd < 0) {
    storage_blocked();
    return -1;
  }
  size_t at = 0;
  while (at < n) {
    ssize_t w = write(fd, text + at, n - at);
    if (w <= 0) {
      close(fd);
      unlink(tmp);
      storage_blocked();
      return -1;
    }
    at += (size_t)w;
  }
  int bad = fsync(fd);
  if (close(fd))
    bad = -1;
  if (!bad && rename(tmp, path))
    bad = -1;
  if (bad) {
    unlink(tmp);
    storage_blocked();
  } else
    fsyncdir(root);
  return bad;
}
static void runtime(void) {
  char b[1024];
  snprintf(
      b, sizeof b,
      "{\"schema_version\":1,\"reader_state\":\"%s\",\"power_state\":\"%s\","
      "\"usb_export\":%s,\"network_connected\":%s,\"idle_since_unix\":%lld,"
      "\"bootstrap_unix\":%lld,\"capture_blocked\":%s,\"coverage_gap\":%s}\n",
      reader, power, usb ? "true" : "false", net ? "true" : "false", idle_since,
      bootstrap, blocked ? "true" : "false", coverage_gap ? "true" : "false");
  if (atomic_text(runtimepath, b))
    perror("runtime checkpoint");
}
static void mark_gap(void) {
  if (!coverage_gap) {
    coverage_gap = 1;
    dirty = 1;
    runtime();
  }
}
/* Startup handshake: adapter may outlive a collector respawn and cache safe
 * states. Ask it to re-observe and replay them; absence never delays startup.
 */
static void request_adapter_refresh(void) {
  char path[PATH_CAP];
  join(path, root, "adapter-control.fifo");
  struct stat before, after;
  if (lstat(path, &before) || !S_ISFIFO(before.st_mode) ||
      before.st_uid != geteuid() || (before.st_mode & 0077))
    return;
  int fd = open(path, O_WRONLY | O_NONBLOCK | O_NOFOLLOW);
  if (fd < 0)
    return;
  if (!fstat(fd, &after) && S_ISFIFO(after.st_mode) &&
      after.st_uid == geteuid() && !(after.st_mode & 0077) &&
      before.st_dev == after.st_dev && before.st_ino == after.st_ino) {
    struct sigaction ignore, previous;
    memset(&ignore, 0, sizeof ignore);
    ignore.sa_handler = SIG_IGN;
    sigemptyset(&ignore.sa_mask);
    if (!sigaction(SIGPIPE, &ignore, &previous)) {
      (void)write(fd, "refresh\n", 8);
      (void)sigaction(SIGPIPE, &previous, NULL);
    }
  }
  close(fd);
}
static unsigned long long queue_bytes(void) {
  unsigned long long n = 0;
  struct stat s;
  if (!stat(activepath, &s))
    n += (unsigned long long)s.st_size;
  DIR *d = opendir(sealedpath);
  if (!d)
    return n;
  struct dirent *e;
  while ((e = readdir(d))) {
    if (e->d_name[0] == '.')
      continue;
    char p[PATH_CAP];
    join(p, sealedpath, e->d_name);
    if (!lstat(p, &s) && S_ISREG(s.st_mode))
      n += (unsigned long long)s.st_size;
  }
  closedir(d);
  return n;
}
static int room(size_t extra) {
  /* Only the directory walk is cached. Firmware and the worker can consume
   * blocks between consecutive input lines, even inside one 30-second pass. */
  if (!fresh_space(extra, reserve_min, 0))
    return 0;
  if (!room_checked || nowsec() < room_checked ||
      nowsec() - room_checked >= 30) {
    queue_cached = queue_bytes();
    room_checked = nowsec();
  }
  if ((unsigned long long)extra > queue_max ||
      queue_cached > queue_max - extra) {
    errno = ENOSPC;
    storage_blocked();
    return 0;
  }
  return 1;
}
static int active_open(void) {
  if (activefd >= 0)
    return 0;
  activefd = open(activepath, O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);
  return activefd < 0 ? -1 : 0;
}
static int save(void) {
  if (active_unsafe) {
    errno = EIO;
    storage_blocked();
    return -1;
  }
  if (activefd >= 0 && fsync(activefd)) {
    storage_blocked();
    return -1;
  }
  struct stat s;
  active_committed = (!stat(activepath, &s) ? s.st_size : 0);
  size_t cap = 256 + (size_t)nc * (PATH_CAP + 100);
  char *b = calloc(1, cap);
  if (!b) {
    storage_blocked();
    return -1;
  }
  size_t at =
      (size_t)snprintf(b, cap, "KSYNC1 %lld %lld %lld %d %d %s %s %d %d\n",
                       bootstrap, idle_since, active_committed, ever_opened,
                       coverage_gap, reader, power, usb, net);
  for (int i = 0; i < nc; i++)
    at += (size_t)snprintf(b + at, cap - at, "%llu %llu %llu %llu %d %s\n",
                           curs[i].dev, curs[i].ino, curs[i].off - curs[i].len,
                           curs[i].gz_size, curs[i].gz_done, curs[i].path);
  /* A normal checkpoint must not borrow the worker's rollback scratch. Only
   * the small emergency runtime marker may use atomic_text's lower floor. */
  if (!fresh_space(strlen(b), reserve_min, 0)) {
    free(b);
    return -1;
  }
  int r = atomic_text(checkpoint, b);
  free(b);
  if (!r) {
    dirty = 0;
    last_sync = monotonic_ms();
  }
  return r;
}
static void seal(void) {
  if (save()) {
    storage_blocked();
    runtime();
    return;
  }
  if (activefd >= 0) {
    close(activefd);
    activefd = -1;
  }
  struct stat s;
  if (stat(activepath, &s) || s.st_size == 0)
    return;
  char dest[PATH_CAP], name[160];
  snprintf(name, sizeof name, "segment-%lld-%ld-%u.log", nowsec(),
           (long)getpid(), serialno++);
  join(dest, sealedpath, name);
  if (rename(activepath, dest)) {
    storage_blocked();
    runtime();
    return;
  }
  fsyncdir(sealedpath);
  char ev[PATH_CAP];
  join(ev, root, "events");
  fsyncdir(ev);
  active_committed = 0;
  if (save()) {
    storage_blocked();
    runtime();
  }
}
static struct cursor *find_cursor(const struct stat *s) {
  for (int i = 0; i < nc; i++)
    if (curs[i].dev == (unsigned long long)s->st_dev &&
        curs[i].ino == (unsigned long long)s->st_ino)
      return &curs[i];
  return NULL;
}
static struct cursor *add_cursor(const char *p, const struct stat *s,
                                 int at_end) {
  struct cursor *c = find_cursor(s);
  if (c)
    return c;
  if (nc == MAX_CURSORS) {
    mark_gap();
    storage_blocked();
    return NULL;
  }
  c = &curs[nc++];
  memset(c, 0, sizeof *c);
  c->dev = (unsigned long long)s->st_dev;
  c->ino = (unsigned long long)s->st_ino;
  c->off = at_end ? (unsigned long long)s->st_size : 0;
  snprintf(c->path, sizeof c->path, "%s", p);
  return c;
}
static int old_event(const char *line) {
  if (strlen(line) < 13 || line[6] != ':')
    return 0;
  struct tm t;
  memset(&t, 0, sizeof t);
  t.tm_isdst = -1;
  char *end = strptime(line, "%y%m%d:%H%M%S", &t);
  return end == line + 13 && (long long)mktime(&t) < bootstrap;
}
static int is_marker(const char *l) {
  return strstr(l, "ReadingTimerController") || strstr(l, "ereader_") ||
         strstr(l, "powerd") || strstr(l, "BufferedLogger") ||
         strstr(l, "JSONJournalSaver") || strstr(l, "ReaderBooklet");
}
static const char *module_payload(const char *line, const char *module) {
  const char *header = strstr(line, ": I ");
  if (!header)
    return NULL;
  header += 4;
  size_t n = strlen(module);
  return !strncmp(header, module, n) ? header + n : NULL;
}
static int exact_message(const char *payload, const char *text) {
  if (!payload)
    return 0;
  size_t n = strlen(text);
  if (strncmp(payload, text, n))
    return 0;
  char c = payload[n];
  return !c || c == '\r' || c == '\n' || c == ';';
}
static int book_event(const char *line, const char *event) {
  const char *p = module_payload(line, "ReadingTimerController:Information::");
  if (!p)
    return 0;
  size_t n = strlen(event);
  if (strncmp(p, event, n))
    return 0;
  char c = p[n];
  return !c || c == ',' || c == ';' || c == '\n' || c == '\r';
}
static void cancel_worker(void);
static void state_reader(const char *s) {
  if (strcmp(reader, s)) {
    snprintf(reader, sizeof reader, "%s", s);
    if (!strcmp(s, "active"))
      cancel_worker();
    idle_since = !strcmp(s, "idle") ? nowsec() : 0;
    idle_mono_ms = monotonic_ms();
    last_worker_mode[0] = 0;
    runtime();
    dirty = 1;
  }
}
static int process_line(const char *line, size_t n) {
  if (!is_marker(line) || (!reading_live && old_event(line)))
    return 0;
  int opens = book_event(line, "OpenBook"),
      closes = book_event(line, "CloseBook");
  int timer = strstr(line, "ReadingTimerController") != NULL;
  if (timer && !opens && !ever_opened)
    return 0;
  /* Keep reading lines only inside explicitly observed windows. BufferedLogger
   * and UTC anchors may arrive after close. */
  /* After a witnessed post-bootstrap open, retain late timer lines too; their
   * event timestamps remain unchanged. */
  if (!room(n)) {
    storage_blocked();
    runtime();
    return -1;
  }
  if (active_open()) {
    storage_blocked();
    runtime();
    return -1;
  }
  off_t begin = lseek(activefd, 0, SEEK_END);
  if (begin < 0) {
    storage_blocked();
    runtime();
    return -1;
  }
  size_t at = 0;
  while (at < n) {
    ssize_t w = write(activefd, line + at, n - at);
    if (w <= 0) {
      int failure = errno;
      if (ftruncate(activefd, begin)) {
        active_unsafe = 1;
        mark_gap();
      }
      storage_blocked();
      runtime();
      errno = failure;
      return -1;
    }
    at += (size_t)w;
  }
  queue_cached += n;
  if (free_cached >= n)
    free_cached -= n;
  dirty = 1;
  const char *booklet = module_payload(line, "ReaderBooklet:Information::");
  if (reading_live &&
      (exact_message(booklet, "ReaderBooklet start") ||
       exact_message(booklet, "Posting reader booklet starting event"))) {
    booklet_stopping = 0;
    state_reader("active");
  }
  if (reading_live && ever_opened && !strcmp(reader, "active") &&
      exact_message(booklet, "ReaderBooklet stop"))
    booklet_stopping = 1;
  if (reading_live && booklet_stopping && ever_opened &&
      exact_message(booklet, "End of stop method")) {
    booklet_stopping = 0;
    state_reader("idle");
    return 1;
  }
  if (opens) {
    booklet_stopping = 0;
    ever_opened = 1;
    if (reading_live)
      state_reader("active");
  }
  if (closes) {
    if (reading_live)
      booklet_stopping = 0;
    if (reading_live && ever_opened)
      state_reader("idle");
    return 1;
  }
  return 0;
}
static int ensure_part(struct cursor *c, size_t need) {
  if (need <= c->capacity)
    return 0;
  size_t cap = c->capacity ? c->capacity * 2 : 1024;
  if (cap < need)
    cap = need;
  if (cap > LINE_CAP)
    cap = LINE_CAP;
  char *buf = realloc(c->part, cap);
  if (!buf) {
    storage_blocked();
    runtime();
    return -1;
  }
  c->part = buf;
  c->capacity = cap;
  return 0;
}
static void release_part(struct cursor *c) {
  if (c->len == 0) {
    free(c->part);
    c->part = NULL;
    c->capacity = 0;
  }
}
static int consume_fd(int fd, struct cursor *c, int seekable) {
  char buf[8192];
  if (seekable && lseek(fd, (off_t)c->off, SEEK_SET) < 0)
    return -1;
  if (!seekable) {
    unsigned long long left = c->off;
    while (left) {
      ssize_t n = read(fd, buf, left < sizeof buf ? (size_t)left : sizeof buf);
      if (n <= 0)
        return -1;
      left -= (unsigned long long)n;
    }
  }
  int want_seal = 0;
  for (;;) {
    ssize_t n = read(fd, buf, sizeof buf);
    if (n == 0)
      break;
    if (n < 0) {
      if (errno == EINTR)
        continue;
      return -1;
    }
    for (ssize_t i = 0; i < n; i++) {
      if (buf[i] == '\n') {
        if (!c->discard) {
          if (ensure_part(c, c->len + 2))
            return -1;
          c->part[c->len++] = '\n';
          c->part[c->len] = 0;
          int result = process_line(c->part, c->len);
          if (result < 0) { /* Leave whole line to reread once space becomes
                               available. */
            if (c->off >= c->len - 1)
              c->off -= c->len - 1;
            c->len = 0;
            release_part(c);
            return -1;
          }
          if (result > 0)
            want_seal = 1;
        }
        c->len = 0;
        c->discard = 0;
      } else if (!c->discard) {
        if (c->len >= LINE_CAP - 2) {
          c->discard = 1;
          c->len = 0;
          mark_gap();
        } else {
          if (ensure_part(c, c->len + 2))
            return -1;
          c->part[c->len++] = buf[i];
        }
      }
      c->off++;
    }
    if (want_seal) {
      seal();
      want_seal = 0;
    }
  }
  release_part(c);
  return 0;
}
static void scan_file(const char *p, int first) {
  struct stat st;
  if (lstat(p, &st) || !S_ISREG(st.st_mode))
    return;
  struct cursor *c = find_cursor(&st);
  size_t plen = strlen(p);
  int gz = plen > 3 && !strcmp(p + plen - 3, ".gz");
  if (!c) {
    if (!first && (long long)st.st_mtime < bootstrap)
      return;
    c = add_cursor(p, &st, first && !gz);
    if (!c)
      return;
    if (first && gz) {
      c->gz_done = 1;
      c->gz_size = (unsigned long long)st.st_size;
      return;
    }
  }
  snprintf(c->path, sizeof c->path, "%s", p);
  if (gz) {
    if (c->gz_done && c->gz_size == (unsigned long long)st.st_size)
      return;
    if (!strcmp(reader, "active"))
      return; /* Catch-up decompression never runs during native reading. */
    /* A bounded one-shot quiet period avoids reading an archive still being
     * compressed. */
    long long mono = monotonic_ms();
    if (!c->gz_probed_ms ||
        c->gz_probe_size != (unsigned long long)st.st_size) {
      c->gz_probe_size = (unsigned long long)st.st_size;
      c->gz_probed_ms = mono;
    }
    if (mono - c->gz_probed_ms < 1000) {
      long long due = c->gz_probed_ms + 1000;
      if (!gzip_due_ms || due < gzip_due_ms)
        gzip_due_ms = due;
      return;
    }
    int pipefd[2];
    if (pipe(pipefd))
      return;
    pid_t child = fork();
    if (child == 0) {
      close(pipefd[0]);
      dup2(pipefd[1], STDOUT_FILENO);
      close(pipefd[1]);
      int nullfd = open("/dev/null", O_WRONLY);
      if (nullfd >= 0) {
        dup2(nullfd, STDERR_FILENO);
        close(nullfd);
      }
      execlp("gzip", "gzip", "-cd", "--", p, (char *)NULL);
      _exit(127);
    }
    close(pipefd[1]);
    if (child < 0) {
      close(pipefd[0]);
      return;
    }
    reading_live = 0;
    int ok = consume_fd(pipefd[0], c, 0);
    close(pipefd[0]);
    int status = 0;
    waitpid(child, &status, 0);
    if (!ok && WIFEXITED(status) && WEXITSTATUS(status) == 0) {
      c->gz_done = 1;
      c->gz_size = (unsigned long long)st.st_size;
    } else {
      mark_gap();
      c->gz_done = 1;
      c->gz_size = (unsigned long long)st.st_size;
    }
    return;
  }
  if (c->off > (unsigned long long)st.st_size) {
    c->off = 0;
    c->len = 0;
    c->discard = 0;
  }
  int fd = open(p, O_RDONLY | O_NOFOLLOW);
  if (fd < 0)
    return;
  reading_live = !strcmp(p, logpath);
  if (reading_live) {
    if (livefd >= 0 && (live_dev != c->dev || live_ino != c->ino)) {
      if (dirty && save()) {
        storage_blocked();
        runtime();
        close(fd);
        return;
      }
      close(livefd);
    } else if (livefd >= 0)
      close(fd);
    if (livefd < 0 || live_dev != c->dev || live_ino != c->ino)
      livefd = fd;
    live_dev = c->dev;
    live_ino = c->ino;
    fcntl(livefd, F_SETFD, FD_CLOEXEC);
    consume_fd(livefd, c, 1);
  } else {
    consume_fd(fd, c, 1);
    close(fd);
  }
}
static int cmp_names(const void *a, const void *b) {
  struct stat sa, sb;
  if (stat(*(char *const *)a, &sa) || stat(*(char *const *)b, &sb))
    return strcmp(*(char *const *)a, *(char *const *)b);
  if (sa.st_mtime < sb.st_mtime)
    return -1;
  if (sa.st_mtime > sb.st_mtime)
    return 1;
  return strcmp(*(char *const *)a, *(char *const *)b);
}
static void scan_dir(const char *dir, int first) {
  DIR *d = opendir(dir);
  if (!d)
    return;
  char *names[MAX_CURSORS];
  int count = 0;
  struct dirent *e;
  while ((e = readdir(d)) && count < MAX_CURSORS) {
    if (strncmp(e->d_name, logbase, strlen(logbase)))
      continue;
    char *p = malloc(PATH_CAP);
    if (!p)
      break;
    join(p, dir, e->d_name);
    names[count++] = p;
  }
  closedir(d);
  qsort(names, (size_t)count, sizeof names[0], cmp_names);
  for (int i = 0; i < count; i++) {
    scan_file(names[i], first);
    free(names[i]);
    if (blocked) {
      for (int j = i + 1; j < count; j++)
        free(names[j]);
      break;
    }
  }
}
static void scan_all(int first) {
  /* Hold the live inode across rename/compress/unlink until its final bytes
   * have been drained. */
  if (livefd >= 0) {
    struct stat held, current;
    if (!fstat(livefd, &held)) {
      struct cursor *c = find_cursor(&held);
      reading_live = !stat(logpath, &current) &&
                     current.st_ino == held.st_ino &&
                     current.st_dev == held.st_dev;
      if (c)
        consume_fd(livefd, c, 1);
    }
  }
  if (blocked)
    return;
  if (strcmp(logdir, rotated_dir))
    scan_dir(rotated_dir, first);
  if (!blocked)
    scan_dir(logdir, first);
  if (blocked)
    return;
  /* Reap only after draining held input and durable output. A replaced pathname
   * is not the same source: otherwise one stale cursor leaks per log rotation.
   */
  int removed = 0;
  for (int i = 0; i < nc;) {
    struct stat st;
    int missing = stat(curs[i].path, &st);
    int replaced = !missing && ((unsigned long long)st.st_dev != curs[i].dev ||
                                (unsigned long long)st.st_ino != curs[i].ino);
    int held =
        curs[i].dev == live_dev && curs[i].ino == live_ino && livefd >= 0;
    if (!held && ((missing && errno == ENOENT) || replaced)) {
      if (curs[i].len || curs[i].discard)
        mark_gap();
      if (dirty && save()) {
        storage_blocked();
        runtime();
        return;
      }
      free(curs[i].part);
      memmove(&curs[i], &curs[i + 1], (size_t)(nc - i - 1) * sizeof curs[0]);
      nc--;
      removed = 1;
    } else
      i++;
  }
  if (removed && save()) {
    storage_blocked();
    runtime();
  }
}
static void load_checkpoint(void) {
  FILE *f = fopen(checkpoint, "r");
  if (!f)
    return;
  char magic[16];
  if (fscanf(f, "%15s %lld %lld %lld %d %d %15s %15s %d %d\n", magic,
             &bootstrap, &idle_since, &active_committed, &ever_opened,
             &coverage_gap, reader, power, &usb, &net) != 10 ||
      strcmp(magic, "KSYNC1")) {
    fclose(f);
    die("invalid checkpoint");
  }
  while (nc < MAX_CURSORS) {
    struct cursor *c = &curs[nc];
    if (fscanf(f, "%llu %llu %llu %llu %d %4095[^\n]\n", &c->dev, &c->ino,
               &c->off, &c->gz_size, &c->gz_done, c->path) != 6)
      break;
    nc++;
  }
  fclose(f); /* Never trust stale network/power observations after process
                restart. */
  snprintf(power, sizeof power, "unknown");
  net = 0;
  usb = 0;
  if (active_committed >= 0) {
    int fd = open(activepath, O_WRONLY | O_NOFOLLOW);
    if (fd >= 0) {
      struct stat s;
      if (!fstat(fd, &s) && s.st_size > active_committed)
        ftruncate(fd, (off_t)active_committed);
      close(fd);
    }
  }
  for (int i = 0; i < nc; i++) { /* Incomplete lines are intentionally replayed
                                    from disk after restart. */
    curs[i].len = 0;
  }
}
static void cancel_worker(void) {
  if (worker > 0)
    kill(worker, SIGTERM);
}
static void control_line(char *l) {
  char a[32], b[32];
  if (sscanf(l, "%31s %31s", a, b) != 2)
    return;
  if (!strcmp(a, "reader")) { /* Test/adapter events require explicit OpenBook,
                                 never inactivity. */
    if (!strcmp(b, "active")) {
      ever_opened = 1;
      state_reader("active");
    } else if (!strcmp(b, "idle") && ever_opened) {
      state_reader("idle");
      seal();
    }
  } else if (!strcmp(a, "power") &&
             (!strcmp(b, "awake") || !strcmp(b, "sleeping"))) {
    snprintf(power, sizeof power, "%s", b);
    if (!strcmp(b, "sleeping")) {
      cancel_worker();
      seal();
    }
  } else if (!strcmp(a, "usb") && (!strcmp(b, "0") || !strcmp(b, "1"))) {
    usb = atoi(b);
    if (usb) {
      cancel_worker();
      seal();
    }
  } else if (!strcmp(a, "network") && (!strcmp(b, "0") || !strcmp(b, "1"))) {
    net = atoi(b);
    if (!net)
      cancel_worker();
    last_worker_mode[0] = 0;
  } else if (!strcmp(a, "rescan")) {
    mark_gap();
  } else if (!strcmp(a, "maintenance") && !strcmp(b, "acknowledge-gap")) {
    coverage_gap = 0;
  } else if (!strcmp(a, "maintenance") && !strcmp(b, "retry")) {
    blocked = 0;
    room_checked = 0;
    last_worker_mode[0] = 0;
  }
  runtime();
  dirty = 1;
}
static void controls(void) {
  static char buf[1024];
  static size_t len;
  char b[512];
  ssize_t n;
  while ((n = read(controlfd, b, sizeof b)) > 0)
    for (ssize_t i = 0; i < n; i++) {
      if (b[i] == '\n') {
        buf[len] = 0;
        control_line(buf);
        len = 0;
      } else if (len < sizeof buf - 1)
        buf[len++] = b[i];
      else
        len = 0;
    }
}
static void maybe_worker(void) {
  if (worker > 0) {
    int status;
    pid_t r = waitpid(worker, &status, WNOHANG);
    if (r == worker) {
      worker = 0;
      room_checked = 0;
    } else
      return;
  }
  if (blocked || usb || strcmp(reader, "idle") || strcmp(power, "awake") ||
      monotonic_ms() < idle_mono_ms + 20000)
    return;
  const char *mode = net ? "sync" : "collect";
  if (!strcmp(last_worker_mode, mode))
    return;
  seal();
  runtime();
  if (blocked)
    return;
  char exe[PATH_CAP];
  join(exe, root, "bin/ksync-worker");
  if (access(exe, X_OK))
    return;
  worker = fork();
  if (worker == 0) {
    execl(exe, exe, "--root", root, mode, (char *)NULL);
    _exit(127);
  }
  if (worker > 0)
    snprintf(last_worker_mode, sizeof last_worker_mode, "%s", mode);
}
static void usage(void) {
  fprintf(stderr,
          "ksync-collector [--root DIR] [--log FILE] [--emit 'power awake'] "
          "[--queue-bytes N] [--reserve-bytes N] [--poll-ms N (test only)]\n");
  exit(2);
}
int main(int argc, char **argv) {
  char *emit = NULL;
  for (int i = 1; i < argc; i++) {
    if (i + 1 >= argc)
      usage();
    char *k = argv[i++], *v = argv[i];
    if (!strcmp(k, "--root"))
      snprintf(root, sizeof root, "%s", v);
    else if (!strcmp(k, "--log"))
      snprintf(logpath, sizeof logpath, "%s", v);
    else if (!strcmp(k, "--emit"))
      emit = v;
    else if (!strcmp(k, "--rotated-dir"))
      snprintf(rotated_dir, sizeof rotated_dir, "%s", v);
    else if (!strcmp(k, "--queue-bytes"))
      queue_max = strtoull(v, NULL, 10);
    else if (!strcmp(k, "--reserve-bytes"))
      reserve_min = strtoull(v, NULL, 10);
    else if (!strcmp(k, "--poll-ms"))
      poll_ms = atoi(v);
    else
      usage();
  }
  if (root[0] != '/' || logpath[0] != '/' || rotated_dir[0] != '/' ||
      strchr(root, '\n') || strchr(logpath, '\n') || strchr(rotated_dir, '\n'))
    usage();
  /* Never retain a userstore cwd across USB mass-storage export. */
  if (chdir("/"))
    die("safe cwd");
  char fifo[PATH_CAP];
  join(fifo, root, "control.fifo");
  if (emit) {
    int fd = open(fifo, O_WRONLY | O_NONBLOCK | O_NOFOLLOW);
    if (fd < 0)
      die("collector control");
    struct stat st;
    if (fstat(fd, &st) || !S_ISFIFO(st.st_mode) || st.st_uid != geteuid() ||
        (st.st_mode & 0077)) {
      close(fd);
      errno = EPERM;
      die("unsafe collector control");
    }
    dprintf(fd, "%s\n", emit);
    close(fd);
    return 0;
  }
  umask(0077);
  if (mkdir_private(root))
    die("private root");
  char p[PATH_CAP];
  join(p, root, "events");
  if (mkdir_private(p))
    die("events");
  join(sealedpath, p, "sealed");
  if (mkdir_private(sealedpath))
    die("sealed");
  join(activepath, p, "active.log");
  join(checkpoint, root, "collector.checkpoint");
  join(runtimepath, root, "runtime.json");
  join(p, root, "collector.lock");
  lockfd = open(p, O_RDWR | O_CREAT | O_NOFOLLOW, 0600);
  if (lockfd < 0 || flock(lockfd, LOCK_EX | LOCK_NB))
    die("collector already running");
  fcntl(lockfd, F_SETFD, FD_CLOEXEC);
  snprintf(logdir, sizeof logdir, "%s", logpath);
  char *slash = strrchr(logdir, '/');
  if (!slash)
    usage();
  snprintf(logbase, sizeof logbase, "%s", slash + 1);
  *slash = 0;
  bootstrap = nowsec();
  last_sync = monotonic_ms();
  load_checkpoint();
  idle_mono_ms = monotonic_ms();
  if (!nc) {
    scan_all(1);
    if (save())
      die("initial checkpoint");
  } else {
    for (int i = 0; i < nc; i++) {
      struct stat s;
      if (!stat(curs[i].path, &s) && s.st_ino == (ino_t)curs[i].ino &&
          s.st_dev == (dev_t)curs[i].dev)
        continue;
      mark_gap();
    }
  }
  if (mkfifo(fifo, 0600) && errno != EEXIST)
    die("control fifo");
  struct stat fs;
  if (lstat(fifo, &fs) || !S_ISFIFO(fs.st_mode) || fs.st_uid != geteuid() ||
      (fs.st_mode & 0077))
    die("unsafe control fifo");
  controlfd = open(fifo, O_RDWR | O_NONBLOCK | O_NOFOLLOW);
  if (controlfd < 0)
    die("control fifo");
  fcntl(controlfd, F_SETFD, FD_CLOEXEC);
  runtime();
  request_adapter_refresh();
  signal(SIGTERM, on_signal);
  signal(SIGINT, on_signal);
  nice(19);
#ifdef __linux__
#ifdef SYS_ioprio_set
  /* Best effort: old kernels may not expose I/O priority. Never block native
   * reads. */
  syscall(SYS_ioprio_set, 1, 0, 3 << 13);
#endif
  notifyfd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
  if (notifyfd >= 0)
    inotify_add_watch(notifyfd, logdir,
                      IN_MODIFY | IN_CREATE | IN_MOVED_TO | IN_DELETE |
                          IN_ATTRIB | IN_Q_OVERFLOW);
  if (notifyfd >= 0 && strcmp(logdir, rotated_dir))
    inotify_add_watch(notifyfd, rotated_dir,
                      IN_MODIFY | IN_CREATE | IN_MOVED_TO | IN_DELETE |
                          IN_ATTRIB | IN_Q_OVERFLOW);
#else
  if (!poll_ms) {
    fprintf(stderr, "Production collector requires Linux inotify. Use "
                    "--poll-ms only for tests.\n");
    return 2;
  }
#endif
  if (notifyfd < 0 && !poll_ms) {
    fprintf(stderr, "inotify unavailable; refusing busy-poll fallback.\n");
    return 2;
  }
  while (!stopping) {
    int timeout = -1;
    long long n = monotonic_ms();
    if (dirty && !blocked)
      timeout = (int)(last_sync + 30000 - n);
    if (!strcmp(reader, "idle") &&
        strcmp(last_worker_mode, net ? "sync" : "collect") &&
        n < idle_mono_ms + 20000) {
      int due = (int)(idle_mono_ms + 20000 - n);
      if (timeout < 0 || due < timeout)
        timeout = due;
    }
    if (gzip_due_ms) {
      int due = (int)(gzip_due_ms - n);
      if (due < 0)
        due = 0;
      if (timeout < 0 || due < timeout)
        timeout = due;
    }
    if (worker > 0 && (timeout < 0 || timeout > 1000))
      timeout = 1000;
    if (poll_ms && (timeout < 0 || timeout > poll_ms))
      timeout = poll_ms;
    if (timeout < 0 && dirty && !blocked)
      timeout = 0;
    struct pollfd fds[2] = {{controlfd, POLLIN, 0}, {notifyfd, POLLIN, 0}};
    int r = poll(fds, notifyfd >= 0 ? 2 : 1, timeout);
    if (r < 0 && errno != EINTR)
      break;
    if (stopping)
      break;
    int was_blocked = blocked;
    if (fds[0].revents & POLLIN)
      controls();
    int changed = poll_ms > 0 || (fds[0].revents & POLLIN);
    if (gzip_due_ms && monotonic_ms() >= gzip_due_ms) {
      changed = 1;
      gzip_due_ms = 0;
    }
#ifdef __linux__
    if (notifyfd >= 0 && (fds[1].revents & POLLIN)) {
      char b[8192];
      ssize_t got;
      while ((got = read(notifyfd, b, sizeof b)) > 0) {
        size_t at = 0;
        while (at < (size_t)got) {
          struct inotify_event *e = (struct inotify_event *)(b + at);
          if (e->mask & IN_Q_OVERFLOW)
            mark_gap();
          at += sizeof *e + e->len;
        }
      }
      changed = 1;
    }
#endif
    if (changed && !blocked && !usb) {
      int was_active = !strcmp(reader, "active");
      scan_all(0);
      if (was_active && !strcmp(reader, "idle") && !blocked)
        scan_dir(rotated_dir, 0);
    }
    if (blocked && !was_blocked) {
      /* One bounded durability attempt after the failed line has been rewound;
       * never turn a persistent low-space condition into a zero-timeout loop.
       */
      (void)save();
      runtime();
    } else if (dirty && !blocked && monotonic_ms() >= last_sync + 30000) {
      if (save())
        runtime();
    }
    maybe_worker();
  }
  cancel_worker();
  if (worker > 0)
    waitpid(worker, NULL, 0);
  seal();
  runtime();
  close(controlfd);
  if (livefd >= 0)
    close(livefd);
  if (notifyfd >= 0)
    close(notifyfd);
  close(lockfd);
  return 0;
}
