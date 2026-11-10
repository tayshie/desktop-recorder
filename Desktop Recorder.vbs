' Desktop Recorder - launch without a console window
Option Explicit

Dim fso, sh, dir, exe
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir

exe = ResolvePythonw()
sh.Run """" & exe & """ """ & dir & "\recorder.py""", 0, False


' Prefer pythonw.exe from PATH, then the usual per-user install locations.
Function ResolvePythonw()
    Dim exec, line, candidates, c
    On Error Resume Next
    Set exec = sh.Exec(sh.ExpandEnvironmentStrings("%COMSPEC%") & " /c where pythonw.exe")
    If Not exec Is Nothing Then
        Do While exec.Status = 0
            WScript.Sleep 30
        Loop
        line = exec.StdOut.ReadLine()
        If Len(line) > 0 Then
            ResolvePythonw = line
            Exit Function
        End If
    End If
    On Error GoTo 0

    candidates = Array( _
        sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\miniconda3\pythonw.exe", _
        sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\anaconda3\pythonw.exe", _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe", _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe", _
        sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe")

    For Each c In candidates
        If fso.FileExists(c) Then
            ResolvePythonw = c
            Exit Function
        End If
    Next

    ResolvePythonw = "pythonw.exe"
End Function
