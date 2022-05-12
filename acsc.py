from auth import *
import time
import servicemanager
import socket
import sys
import win32event
import win32service
import win32serviceutil
import pypyodbc
from datetime import datetime
from os import path, mkdir
from fast_bitrix24 import Bitrix
import fdb
from threading import Thread

_list = []
stop = False
query_depth_mssql = 30
query_depth_fbd = 30


def write_log(text, type_log):
    """Пишет лог на коннекты и реконнекты"""
    log = "C:\\ACSC Logs"
    if not path.exists(log):
        mkdir(log)
    cur_time = str(datetime.now().strftime("%d.%m.%Y %H:%M:%S"))
    with open(f'{log}\\{type_log}.log', 'a') as f:
        f.write(f'{cur_time} : {text}\n')


class Connector(win32serviceutil.ServiceFramework):
    _svc_name_ = "ACSC"
    _svc_display_name_ = "ACS Connector"
    _svc_description_ = "Получение данных из СКУД и загрузка их на сервер СУД"
    bitrix = Bitrix(webhook)

    def __init__(self, args):
        try:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
            socket.setdefaulttimeout(60)
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def connect(self, _type, _server, _db, _login, _pass):
        """Коннектится к нужной БД"""
        try:
            self.con = object
            if _type == 'mssql':
                self.con = pypyodbc.connect(
                    f'DRIVER=SQL Server;SERVER={_server};DATABASE={_db};UID={_login};PWD={_pass}')
            elif _type == 'fdb':
                self.con = fdb.connect(host=_server, database=_db, user=_login, password=_pass, charset='none')

            return self.con
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def get_user_data(self, con, _type):
        """Получаент данные по сотрудникам за -30 секунд плюс время простоя сервера с момента запуска"""
        try:
            global _list, query_depth_mssql, query_depth_fbd
            journals = []
            cursor = con.cursor()
            if _type == 'mssql':
                cursor.execute(
                    f"select * from Journals WHERE dateadd(SECOND, (SystemDate / 10000000) - 11644473600, convert(datetime, '1-1-1970 03:00:00')) > DATEADD(SECOND, -{query_depth_mssql}, GETDATE()) AND UserName IS NOT NULL AND CardNo != '0' AND EmployeeUID IN (SELECT UID FROM Employees where DepartmentUID IS NOT NULL)"
                )
                query_depth_mssql = 30
            elif _type == 'fdb':
                cursor.execute(f"""
                        SELECT FB_EVN.DT, t2.NAME, t3.LNAME, t3.FNAME, t3.SNAME, t3.DOP1
                        FROM FB_EVN, FB_DVS t2, FB_USR t3
                        WHERE FB_EVN.DVS = t2.id AND FB_EVN.USR = t3.id AND DOP1 = 'АУП' AND DT > DATEADD(SECOND, -{query_depth_fbd}, CURRENT_TIME);
                        """
                               )
                query_depth_fbd = 30
            for row in cursor:
                if _type == 'mssql':
                    app_time = datetime.fromtimestamp((row[13] // 10000000) - 11644473600)
                    journals.append([app_time.strftime('%d.%m.%Y %H:%M:%S'), row[14], row[9]])
                elif _type == 'fdb':
                    journals.append(
                        [row[0].strftime('%d.%m.%Y %H:%M:%S'), row[2] + " " + row[3] + " " + row[4], row[1]])

            if len(_list) > 5000:
                for record in _list[:-500]:
                    _list.remove(record)
            if journals is not None:
                for u in journals:
                    if u not in _list:
                        _list.append(u)
                        self.write_data(u)
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def write_data(self, data: list):
        """Записивые полученные данные в справочник СУД"""
        try:
            self.bitrix.call('lists.element.add',
                             [
                                 {
                                     'IBLOCK_TYPE_ID': 'bitrix_processes',
                                     'IBLOCK_ID': '153',
                                     'ELEMENT_CODE': f'{data[1]} {data[0]}',
                                     'FIELDS': {
                                         'NAME': f'{data[1]} {data[0]}',
                                         'PROPERTY_812': f'{data[1]}',
                                         'PROPERTY_815': f'{data[0]}',
                                         'PROPERTY_793': f'{data[2]}',
                                         'PROPERTY_814': 'Импортировано',
                                     }
                                 }
                             ]
                             )
            el = self.check_entry(data)
            if el != ([],):
                u = data[1] + " - " + data[2]
                write_log(u, "registered")
            else:
                exc = sys.exc_info()[1]
                write_log(exc.args[0], "errors")
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def check_entry(self, data: list):
        """Проверяет записана ли запись в Битрикс"""
        element = self.bitrix.call('lists.element.get',
                                   [
                                       {
                                           'IBLOCK_TYPE_ID': 'bitrix_processes',
                                           'IBLOCK_ID': '153',
                                           'ELEMENT_CODE': f'{data[1]} {data[0]}'
                                       }
                                   ]
                                   )
        return element

    def delay(self, t: int):
        """Задержка между действиями"""
        try:
            global stop
            for i in range(t):
                if not stop:
                    time.sleep(1)
                else:
                    break
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def loop(self, _type, _server, _db, _login, _pass):
        """Главный цикл и обработка ошибок"""
        try:
            global stop, query_depth_mssql, query_depth_fbd
            ms_time = time.time()
            stop = False
            write_log("Служба запущена", "journal")
            while not stop:
                try:
                    con = self.connect(_type, _server, _db, _login, _pass)
                except pypyodbc.DatabaseError:
                    write_log(f"Связь с сервером {_type} не установлена", "journal")
                    self.delay(10)
                    continue
                except fdb.fbcore.DatabaseError:
                    write_log(f"Связь с сервером {_type} не установлена", "journal")
                    self.delay(10)
                    continue
                else:
                    if _type == 'mssql':
                        query_depth_mssql += (time.time() - ms_time)
                    elif _type == 'fdb':
                        query_depth_fbd += (time.time() - ms_time)
                    write_log(f"Соединение с {_type} установлено", "journal")

                    while not stop:
                        try:
                            self.get_user_data(con, _type)
                            self.delay(10)
                        except pypyodbc.ProgrammingError:
                            _time = time.time()
                            write_log(f"Связь с сервером {_type} потеряна", "journal")
                            try:
                                con.close()
                            except pypyodbc.DatabaseError:
                                self.delay(10)
                                break
                            self.delay(10)
                            break
                        except pypyodbc.DatabaseError:
                            _time = time.time()
                            write_log(f"Связь с сервером {_type} потеряна", "journal")
                            try:
                                con.close()
                            except pypyodbc.DatabaseError:
                                self.delay(10)
                                break
                            self.delay(10)
                            break
                        except fdb.fbcore.ProgrammingError:
                            _time = time.time()
                            write_log(f"Связь с сервером {_type} потеряна", "journal")
                            con.close()
                            self.delay(10)
                            break
                        except fdb.fbcore.DatabaseError:
                            _time = time.time()
                            write_log(f"Связь с сервером {_type} потеряна", "journal")
                            con.close()
                            self.delay(10)
                            break
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def SvcStop(self):
        """Остановка службы"""
        try:
            global stop
            stop = True
            write_log("Служба остановлена", "journal")
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.hWaitStop)
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")

    def SvcDoRun(self):
        """Старт службы"""
        try:
            self.t1 = Thread(target=self.loop, args=('mssql', mssql_server, sql_db, sql_login, sql_pass), daemon=True)

            self.t2 = Thread(target=self.loop, args=('fdb', fb_host_ip, fb_db_path, fb_login, fb_pass), daemon=True)
            self.t1.start()
            self.t2.start()
            self.t1.join()
            self.t2.join()
        except Exception:
            e = sys.exc_info()[1]
            write_log(e.args[0], "errors")


if __name__ == '__main__':
    write_log("Служба запущена", "journal")
    try:
        if len(sys.argv) == 1:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(Connector)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            win32serviceutil.HandleCommandLine(Connector)
    except Exception:
        e = sys.exc_info()[1]
        write_log(e.args[0], "errors")
