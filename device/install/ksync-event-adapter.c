/* SPDX-License-Identifier: AGPL-3.0-only
 * Passive event adapter. State changes only; no timer/network polling or sleep inhibition.
 * Four blocking firmware LIPC listeners form part of the measured process-tree budget.
 */
#define _POSIX_C_SOURCE 200809L
#define _GNU_SOURCE
#ifdef __APPLE__
#define _DARWIN_C_SOURCE
#endif
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <poll.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <time.h>

static const char *root="/var/local/kindle-native-sync";
static volatile sig_atomic_t quit=0;
static pid_t children[4];
static int power_state=-1, network_state=-1, usb_state=-1;
static uid_t owner=0;
static void stop_signal(int sig){(void)sig;quit=1;}

static int capture(const char *source,const char *property,char *out,size_t size){
    int fds[2];if(pipe(fds))return -1;
    pid_t pid=fork();if(pid<0){close(fds[0]);close(fds[1]);return -1;}
    if(!pid){
        dup2(fds[1],1);close(fds[0]);close(fds[1]);
        int null=open("/dev/null",O_WRONLY);if(null>=0){dup2(null,2);close(null);}
        execlp("lipc-get-prop","lipc-get-prop",source,property,(char*)0);_exit(127);
    }
    close(fds[1]);size_t n=0;
    while(n+1<size){
        struct pollfd waitfd={fds[0],POLLIN,0};
        int ready=poll(&waitfd,1,2000);
        if(ready<=0){kill(pid,SIGKILL);close(fds[0]);waitpid(pid,0,0);out[0]=0;return -1;}
        ssize_t got=read(fds[0],out+n,size-1-n);
        if(got<=0)break;
        n+=(size_t)got;
    }
    close(fds[0]);int status;if(waitpid(pid,&status,0)<0)return -1;
    out[n]=0;while(n&&(out[n-1]=='\n'||out[n-1]=='\r'))out[--n]=0;
    return WIFEXITED(status)&&WEXITSTATUS(status)==0?0:-1;
}

static int emit(const char *line){
    char path[512];snprintf(path,sizeof path,"%s/control.fifo",root);
    struct stat s;if(lstat(path,&s)||!S_ISFIFO(s.st_mode)||s.st_uid!=owner||(s.st_mode&077))return -1;
    int fd=open(path,O_WRONLY|O_NONBLOCK|O_NOFOLLOW);if(fd<0)return -1;
    size_t len=strlen(line);ssize_t n=write(fd,line,len);close(fd);
    return n==(ssize_t)len?0:-1;
}

static void set_power(int value){if(value!=power_state&&!emit(value?"power awake\n":"power sleeping\n"))power_state=value;}
static void set_network(int value){if(value!=network_state&&!emit(value?"network 1\n":"network 0\n"))network_state=value;}
static void set_usb(int value){if(value!=usb_state&&!emit(value?"usb 1\n":"usb 0\n"))usb_state=value;}
static void refresh_power(void){
    char value[128];set_power(capture("com.lab126.powerd","state",value,sizeof value)==0&&!strcmp(value,"active"));
}
static void refresh_network(void){
    char value[128];set_network(capture("com.lab126.wifid","cmState",value,sizeof value)==0&&!strcmp(value,"CONNECTED"));
}
static void refresh_usb(void){
    char value[128];
    int drive=capture("com.lab126.volumd","driveModeState",value,sizeof value)==0&&!strcmp(value,"0");
    int mounted=capture("com.lab126.volumd","userstoreIsAvailable",value,sizeof value)==0&&!strcmp(value,"1");
    set_usb(!(drive&&mounted));
}
static void refresh_all(void){refresh_usb();refresh_power();refresh_network();}
static int control_channel(void){
    char path[512];struct stat s;
    if(lstat(root,&s)||!S_ISDIR(s.st_mode)||s.st_uid!=owner||(s.st_mode&077))return -1;
    snprintf(path,sizeof path,"%s/adapter-control.fifo",root);
    if(mkfifo(path,0600)&&errno!=EEXIST)return -1;
    if(lstat(path,&s)||!S_ISFIFO(s.st_mode)||s.st_uid!=owner||(s.st_mode&077))return -1;
    int fd=open(path,O_RDWR|O_NONBLOCK|O_NOFOLLOW);
    if(fd>=0)fcntl(fd,F_SETFD,FD_CLOEXEC);
    return fd;
}
static void forced_snapshot(void){
    /* One atomic fail-closed barrier before any slow property query. A worker
       must never mistake the first safe field for a complete refreshed snapshot. */
    if(emit("usb 1\npower sleeping\nnetwork 0\n"))return;
    power_state=-1;network_state=-1;usb_state=-1;
    refresh_all();
}

static void event_line(int source,const char *line){
    char name[64];size_t n=strcspn(line," \t\r\n");
    if(!n||n>=sizeof name)return;
    memcpy(name,line,n);name[n]=0;
    /* Negative transitions are authoritative and safe to apply immediately.
       Positive transitions require the matching read-only authoritative property.
       Repeated same-state notifications and all unrelated events do no forks. */
    if(source==0){
        if(!strcmp(name,"goingToScreenSaver")||!strcmp(name,"readyToSuspend")||!strcmp(name,"suspending"))set_power(0);
        else if((!strcmp(name,"outOfScreenSaver")||!strcmp(name,"resuming")||!strcmp(name,"wakeupFromSuspend"))&&power_state!=1)refresh_power();
    }else if(source==1){
        if(!strcmp(name,"cmDisconnected")||!strcmp(name,"cmIntfNotAvailable"))set_network(0);
        else if(!strcmp(name,"cmConnected")&&network_state!=1)refresh_network();
        else if(!strcmp(name,"cmStateChange")){
            const char *payload=line+n;while(*payload==' '||*payload=='\t')payload++;
            if(!strcmp(payload,"\"CONNECTED\"")||!strcmp(payload,"CONNECTED")){if(network_state!=1)refresh_network();}
            else if(!strcmp(payload,"\"NA\"")||!strcmp(payload,"\"READY\"")||!strcmp(payload,"\"PENDING\"")||!strcmp(payload,"\"DISCONNECTED\""))set_network(0);
        }
    }else if(source==2){
        if(!strcmp(name,"userstoreUnavailable")||!strcmp(name,"userstoreGoingAway")||!strcmp(name,"userstoreWillBeUnavailable"))set_usb(1);
        else if(!strcmp(name,"userstoreAvailable")&&usb_state!=0)refresh_usb();
    }else if(source==3){
        if(!strcmp(name,"usbConfigured")||!strcmp(name,"usbPlugIn"))set_usb(1);
        else if((!strcmp(name,"usbUnconfigured")||!strcmp(name,"usbPlugOut"))&&usb_state!=0)refresh_usb();
    }
}

int main(int argc,char **argv){
#ifdef KSYNC_TESTING
    if(argc!=3)return 2;
    root=argv[1];owner=getuid();setenv("PATH",argv[2],1);
#else
    (void)argv;if(argc!=1||geteuid()!=0)return 1;
    setenv("PATH","/sbin:/usr/sbin:/bin:/usr/bin",1);
#endif
    if(chdir("/")!=0)return 1;
    signal(SIGTERM,stop_signal);signal(SIGINT,stop_signal);signal(SIGPIPE,SIG_IGN);
    for(int i=0;i<50&&!quit;i++){
        char path[512];struct stat s;snprintf(path,sizeof path,"%s/control.fifo",root);
        if(!lstat(path,&s)&&S_ISFIFO(s.st_mode))break;
        struct timespec delay={0,100000000};nanosleep(&delay,0);
    }
    int control_fd=control_channel();if(control_fd<0)return 1;
    refresh_all();
    const char *sources[]={"com.lab126.powerd","com.lab126.wifid","com.lab126.volumd","com.lab126.hal"};
    struct pollfd fds[5];memset(fds,0,sizeof fds);
    fds[4].fd=control_fd;fds[4].events=POLLIN;
    char lines[5][512];size_t lengths[5]={0};int dropping[5]={0};
    for(int i=0;i<4;i++){
        int p[2];if(pipe(p))goto done;
        pid_t pid=fork();if(pid<0){close(p[0]);close(p[1]);goto done;}
        if(!pid){
            dup2(p[1],1);close(p[0]);close(p[1]);for(int j=0;j<i;j++)close(fds[j].fd);
            int null=open("/dev/null",O_WRONLY);if(null>=0){dup2(null,2);close(null);}
            execlp("lipc-wait-event","lipc-wait-event","-m","-s","0",sources[i],"*",(char*)0);_exit(127);
        }
        children[i]=pid;close(p[1]);fds[i].fd=p[0];fds[i].events=POLLIN;
    }
    refresh_all();
    while(!quit){
        int n=poll(fds,5,-1);if(n<0){if(errno==EINTR)continue;break;}
        for(int i=0;i<5;i++){
            if(fds[i].revents&(POLLERR|POLLHUP|POLLNVAL))goto done;
            if(fds[i].revents&POLLIN){
                char buf[1024];ssize_t got=read(fds[i].fd,buf,sizeof buf);if(got<=0)goto done;
                for(ssize_t k=0;k<got;k++){
                    if(buf[k]=='\n'){
                        if(!dropping[i]){
                            lines[i][lengths[i]]=0;
                            if(i==4){if(!strcmp(lines[i],"refresh"))forced_snapshot();}
                            else event_line(i,lines[i]);
                        }
                        dropping[i]=0;lengths[i]=0;
                    }else if(!dropping[i]){
                        if(lengths[i]+1>=sizeof lines[i]){dropping[i]=1;lengths[i]=0;}
                        else if(buf[k]!='\r')lines[i][lengths[i]++]=buf[k];
                    }
                }
            }
        }
    }
    done:
    set_network(0);set_usb(1);set_power(0);
    for(int i=0;i<4;i++)if(children[i]>0){kill(children[i],SIGTERM);waitpid(children[i],0,0);}
    close(control_fd);
    return quit?0:1;
}
