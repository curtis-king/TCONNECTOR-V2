import win32serviceutil 
import sys 
sys.path.insert(0, "C:\Users\Administrateur\Desktop\connecteur-python\.") 
from service import TConnectorService 
win32serviceutil.HandleCommandLine(TConnectorService) 
