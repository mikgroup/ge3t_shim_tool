from datetime import datetime
import warnings
import sys, paramiko, subprocess, os, threading, re
import numpy as np
import json

import signal
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget, QTextEdit, QLabel, QSlider, QHBoxLayout, QLineEdit, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsTextItem, QTabWidget, QCheckBox, QSizePolicy, QButtonGroup, QRadioButton, QGraphicsItem
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QPixmap, QImage, QDoubleValidator, QIntValidator, QPainter, QPen, QBrush, QColor

# Import the custom client classes and util functions
from exsi_client import exsi
from shim_client import shim
from dicomUtils import *
from shimCompute import *
from utils import *

warnings.filterwarnings("ignore", "Degrees of freedom <= 0 for slice", RuntimeWarning)
warnings.filterwarnings("ignore", "Mean of empty slice", RuntimeWarning)
warnings.filterwarnings("ignore", "Creating an ndarray from ragged nested sequences (which is a list-or-tuple of lists-or-tuples-or ndarrays with different lengths or shapes) is deprecated. If you meant to do this, you must specify 'dtype=object' when creating the ndarray.\n  arr = np.asanyarray(arr)", np.VisibleDeprecationWarning)

class ExsiGui(QMainWindow):
    """
    The ExsiGui class represents the main GUI window for controlling the Exsi system.
    It provides functionality for connecting to the scanner client, the Shim client,
    and performing various operations related to calibration, scanning, and shimming.
    """

    def __init__(self, config):
        super().__init__()

        # tick this only when you continue to be devving...
        self.debugging = True

        self.examDateTime = datetime.now()
        self.examDateString = self.examDateTime.strftime('%Y%m%d_%H%M%S')

        self.config = config
        self.scannerLog = os.path.join(self.config['rootDir'], config['scannerLog'])
        self.shimLog = os.path.join(self.config['rootDir'], config['shimLog'])
        self.guiLog = os.path.join(self.config['rootDir'], config['guiLog'])

        # Create the log files if they don't exist / empty them if they already have things there
        for log in [self.scannerLog, self.shimLog, self.guiLog]:
            if not os.path.exists(log):
                # create the directory and file
                os.makedirs(os.path.dirname(log), exist_ok=True)
            with open(log, "w"):
                pass

        # Start the connection to the Shim client.
        # the requireShimConnection decorator will check if the connection is ready before executing any shim functionality.
        self.shimInstance = shim(self.config['shimPort'], self.config['shimBaudRate'], self.shimLog, debugging=self.debugging)

        # Start the connection to the scanner client.
        # The requireExsiConnection decorator will check if the connection is ready before executing any exsi functionality.
        self.exsiInstance = exsi(self.config['host'], self.config['exsiPort'], self.config['exsiProduct'], self.config['exsiPasswd'],
                                 self.shimInstance.shimZero, self.shimSetCurrentManual, self.scannerLog, debugging=self.debugging)
        
        # connect the clear queue commands so that they can be called from the other client
        self.shimInstance.clearExsiQueue = self.exsiInstance.clear_command_queue
        self.exsiInstance.clearShimQueue = self.shimInstance.clearCommandQueue

        self.shimSliceIndex = 30
        self.roiSliceIndex = 30
        self.basisFunctionIndex = 0
        self.basisSliceIndex = 30 
        
        self.shimDeltaTE = 500
        self.shimCalCurrent = 1.0
    
        self.shimData = [None, None, None] # where the data actually gets saved to 
        self.shimImages = [None, None, None] # what is being displayed. copys and modifies shimData whenever ROI changes
        self.shimImage = self.shimImages[0] # the specific image being displayed for simplicity
        self.basisImages = None
        self.basisImagesSet = False
        self.basisMax = -np.inf
        self.basisMin = -np.inf
        self.lastSliceData = None

        self.shimStatStrs = [None, None, None]
        self.shimStats = [None, None, None]

        # the raw data and actual computed data
        self.rawBasisB0maps = []
        self.basisB0maps = []
        self.currents = None # currents for every single slice
        self.roiMask = None # for when the user draws in the desired 3d ROI
        self.finalMask = None # the actual mask used to compute any shimming and display anything...

        # All the attributes for scan session
        self.assetCalibrationDone = False
        self.autoPrescanDone = False
        self.obtainedBasisMaps = False
        self.computedShimCurrents = False

        # attributes for the ROI editor
        self.currentROIImageData = None
        self.backgroundDCMdir = None
        self.roiEditorEnabled = False
        self.roiDepth = None
        self.newROI = False
        self.roiSliderGranularity = 100 # increase this if you want finer adjustments of the ROI region. Note that it might not do anything if pixels of roi image are too large...
        self.sizes = [0,0,0]
        self.center = [0,0,0]

        # scan parameter attributes
        self.linShimFactor = 20 # Max 300 -- the strength at which to record the basis map for the linear shims
        self.centerFreq = 127.7 # default center frequency
        self.linShims = [0,0,0] # the linear shims to apply to the basis maps

        # TODO: these currently have no use
        self.currentImageTE = None
        self.currentImageOrientation = None

        # generic data
        self.gehcExamDataPath = None
        self.localExamRootDir = None

        # array of buttons that need to be disabled during slow operations in other threads
        self.slowButtons = []
        # Setup the GUI
        self.initUI()
        

    ##### GUI LAYOUT RELATED FUNCTIONS #####   

    def initUI(self):

        self.setWindowAndExamNumber()
        self.setGeometry(100, 100, 1500, 800)

        self.centralTabWidget = QTabWidget()
        self.setCentralWidget(self.centralTabWidget)
        self.centralTabWidget.currentChanged.connect(self.onTabSwitch)

        basicTab = QWidget()
        basicLayout = QVBoxLayout()
        self.setupBasicTabLayout(basicLayout)
        basicTab.setLayout(basicLayout)

        shimmingTab = QWidget()
        shimmingLayout = QVBoxLayout()
        self.setupShimmingTabLayout(shimmingLayout)
        shimmingTab.setLayout(shimmingLayout)

        basisTab = QWidget()
        basisLayout = QVBoxLayout()
        self.setup3rdTabLayout(basisLayout)
        basisTab.setLayout(basisLayout)

        self.setupTabAndWaitForConnection(basicTab, "EXSI Control", self.exsiInstance.connected_ready_event)
        self.setupTabAndWaitForConnection(shimmingTab, "SHIM Control", self.shimInstance.connectedEvent)
        self.centralTabWidget.addTab(basisTab, "Basis/Performance Visualization")
    
    def setupTabAndWaitForConnection(self, tab, tabName, connectedEvent):
        """Add a tab to the central tab widget and dynamically indicate if the client is connected."""
        name = tabName
        if not connectedEvent.is_set():
            name += " [!NOT CONNECTED]"
        self.centralTabWidget.addTab(tab, name)
        # wait for "connected event" to be set and rename the tab name removing the [!NOT CONNECTED]
        def renameTab():
            if not connectedEvent.is_set():
                connectedEvent.wait()
            index = self.centralTabWidget.indexOf(tab)
            self.log(f"Debug: renaming tab {index} to {tabName}")
            self.centralTabWidget.setTabText(index, tabName)
        t = threading.Thread(target=renameTab)
        t.daemon = True
        t.start()

    def setWindowAndExamNumber(self):
        # TODO: still somehow append a date so that you know the data you gen later
        self.guiWindowTitle = lambda: f"[ Shim Control GUI | EXAM: {self.exsiInstance.examNumber or '!'} | Patient: {self.exsiInstance.patientName or '!'} ]"
        self.setWindowTitle(self.guiWindowTitle())
        def setExamNumberAndName():
            if not self.exsiInstance.connected_ready_event.is_set():
                self.exsiInstance.connected_ready_event.wait()
            self.localExamRootDir = os.path.join(self.config['rootDir'], "data", self.exsiInstance.examNumber)
            if not os.path.exists(self.localExamRootDir):
                os.makedirs(self.localExamRootDir)
            # rename the window of the gui to include the exam number and patient name
            self.setWindowTitle(self.guiWindowTitle())
        t = threading.Thread(target=setExamNumberAndName)
        t.daemon = True
        t.start()
    
    def setupBasicTabLayout(self, layout):
        # basic layout is horizontal
        basicLayout = QHBoxLayout()
        layout.addLayout(basicLayout)

        # Add imageLayout to the basicLayout
        imageLayout = QVBoxLayout()
        basicLayout.addLayout(imageLayout)

        # Slider for selecting slices
        self.roiSliceIndexSlider, self.roiSliceIndexEntry = self.addLabeledSliderAndEntry(imageLayout, "Slice Index (Int): ", QIntValidator(0, 0)) #TODO: validate this after somehow....
        self.roiSliceIndexSlider.setValue(self.roiSliceIndex)
        self.roiSliceIndexEntry.setText(str(self.roiSliceIndex))
        self.roiSliceIndexSlider.valueChanged.connect(self.updateFromROISliceSlider)
        self.roiSliceIndexEntry.editingFinished.connect(self.updateFromSliceROIEntry)

        # Setup QGraphicsView for image display
        self.roiScene = QGraphicsScene()
        self.roiViewLabel = QLabel()
        self.roiView = QGraphicsView(self.roiScene)
        self.roiView.setFixedSize(512, 512)  # Set a fixed size for the view
        self.roiView.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        imageLayout.addWidget(self.roiView, alignment=Qt.AlignmentFlag.AlignCenter)
        imageLayout.addWidget(self.roiViewLabel)
        
        # roiPlaceholder text setup, and the actual pixmap item
        self.roiPlaceholderText = QGraphicsTextItem("Waiting for image data")
        self.roiPlaceholderText.setPos(50, 250)  # Position the text appropriately within the scene
        self.roiScene.addItem(self.roiPlaceholderText)
        self.roiPixmapItem = QGraphicsPixmapItem()
        self.roiScene.addItem(self.roiPixmapItem)
        self.roiPixmapItem.setZValue(1)  # Ensure pixmap item is above the roiPlaceholder text

        roiAdjustmentSliderLayout = QHBoxLayout()
        roiSizeSliders = QVBoxLayout()
        roiPositionSliders = QVBoxLayout()
        imageLayout.addLayout(roiAdjustmentSliderLayout)
        roiAdjustmentSliderLayout.addLayout(roiSizeSliders)
        roiAdjustmentSliderLayout.addLayout(roiPositionSliders)

        label = ["X", "Y", "Z"]
        self.roiSizeSliders = [None for _ in range(3)]
        self.roiPositionSliders = [None for _ in range(3)]
        for i in range(3):
            self.roiSizeSliders[i] = self.addLabeledSlider(roiSizeSliders, f"Size {label[i]}")
            self.roiSizeSliders[i].valueChanged.connect(self.visualizeROI)
            self.roiSizeSliders[i].setEnabled(False)
            self.roiPositionSliders[i] = self.addLabeledSlider(roiPositionSliders, f"Center {label[i]}")
            self.roiPositionSliders[i].valueChanged.connect(self.visualizeROI)
            self.roiPositionSliders[i].setEnabled(False)

        self.roiToggleButton = self.addButtonConnectedToFunction(imageLayout, "Enable ROI Editor", self.toggleROIEditor)

        # Controls and log layout
        controlsLayout = QVBoxLayout()
        basicLayout.addLayout(controlsLayout)
        self.setupExsiButtonsAndLog(controlsLayout)

        # Connect the log monitor
        self.exsiLogMonitorThread = LogMonitorThread(self.scannerLog)
        self.exsiLogMonitorThread.update_log.connect(self.updateExsiLogOutput)
        self.exsiLogMonitorThread.start()
        

    def setupExsiButtonsAndLog(self, layout):
        # create the buttons
        self.reconnectExsiButton = self.addButtonConnectedToFunction(layout, "Reconnect EXSI", self.exsiInstance.connectExsi)
        self.doCalibrationScanButton = self.addButtonConnectedToFunction(layout, "Do Calibration Scan", self.doCalibrationScan)
        self.doFgreScanButton = self.addButtonConnectedToFunction(layout, "Do FGRE Scan", self.doFgreScan)
        self.renderLatestDataButton = self.addButtonConnectedToFunction(layout, "Render Data", self.doGetAndSetROIImage)
        self.slowButtons += [self.doCalibrationScanButton, self.doFgreScanButton, self.renderLatestDataButton]

        # radio button group for selecting which roi view you want to see
        roiVizButtonWindow = QWidget()
        roiVizButtonLayout = QHBoxLayout()
        roiVizButtonWindow.setLayout(roiVizButtonLayout)
        self.roiVizButtonGroup = QButtonGroup(roiVizButtonWindow)
        layout.addWidget(roiVizButtonWindow)

        # Create the radio buttons
        roiLatestDataButton = QRadioButton("Latest Data")
        roiBackgroundButton = QRadioButton("Background")
        roiLatestDataButton.setChecked(True)  # Default to latest data
        roiBackgroundButton.setEnabled(False)  # Disable the background button for now
        roiVizButtonLayout.addWidget(roiLatestDataButton)
        roiVizButtonLayout.addWidget(roiBackgroundButton)
        self.roiVizButtonGroup.addButton(roiLatestDataButton, 0)
        self.roiVizButtonGroup.addButton(roiBackgroundButton, 1)
        self.roiVizButtonGroup.idClicked.connect(self.toggleROIBackgroundImage)

        self.exsiLogOutput = QTextEdit()
        self.exsiLogOutput.setReadOnly(True)
        self.exsiLogOutputLabel = QLabel("EXSI Log Output")

        layout.addWidget(self.exsiLogOutputLabel)
        layout.addWidget(self.exsiLogOutput)

    def setupShimmingTabLayout(self, layout):
        # Controls and log layout
        shimLayout = QVBoxLayout()

        self.setupShimButtonsAndLog(shimLayout)
        
        # Connect the log monitor
        self.shimLogMonitorThread = LogMonitorThread(self.shimLog)
        self.shimLogMonitorThread.update_log.connect(self.updateShimLogOutput)
        self.shimLogMonitorThread.start()
        
        # Add the basic layout to the provided layout
        layout.addLayout(shimLayout)

    def setupShimButtonsAndLog(self, layout):
        # shim window is split down the middle to begin with
        shimSplitLayout = QHBoxLayout()
        layout.addLayout(shimSplitLayout)

        # make the left and right vboxex
        leftLayout = QVBoxLayout()
        rightLayout = QVBoxLayout()
        shimSplitLayout.addLayout(leftLayout)
        shimSplitLayout.addLayout(rightLayout)

        # LEFT SIDE

        # add radio button group for selecting which view you want to see....
        # Create the QButtonGroup
        shimVizButtonWindow = QWidget()
        shimVizButtonLayout = QHBoxLayout()
        shimVizButtonWindow.setLayout(shimVizButtonLayout)
        self.shimVizButtonGroup = QButtonGroup(shimVizButtonWindow)
        leftLayout.addWidget(shimVizButtonWindow)

        # Create the radio buttons
        shimVizButtonBackground = QRadioButton("Background")
        shimVizButtonEstBackground = QRadioButton("Estimated Shimmed Background")
        shimVizButtonShimmedBackground = QRadioButton("Actual Shimmed Background")
        shimVizButtonBackground.setChecked(True)  # Default to background

        # Add the radio buttons to the button group
        self.shimVizButtonGroup.addButton(shimVizButtonBackground, 0)
        shimVizButtonLayout.addWidget(shimVizButtonBackground)
        self.shimVizButtonGroup.addButton(shimVizButtonEstBackground, 1)
        shimVizButtonLayout.addWidget(shimVizButtonEstBackground)
        self.shimVizButtonGroup.addButton(shimVizButtonShimmedBackground, 2)
        shimVizButtonLayout.addWidget(shimVizButtonShimmedBackground)
        self.shimVizButtonGroup.idClicked.connect(self.toggleShimImage)

        # add another graphics scene visualizer
        # Setup QGraphicsView for image display
        self.shimViewLabel = QLabel()
        self.shimView = ImageViewer(self, self.shimViewLabel)
        leftLayout.addWidget(self.shimView, alignment=Qt.AlignmentFlag.AlignCenter)
        leftLayout.addWidget(self.shimViewLabel)
        self.shimView.setFixedSize(512, 512)  # Set a fixed size for the view
        self.shimView.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)

        # add the statistics output using another non editable textedit widget
        shimStatTextLabel = QLabel("Image Statistics")
        shimBackStatText = QTextEdit()
        shimBackStatText.setReadOnly(True)
        shimExpectStatText = QTextEdit()
        shimExpectStatText.setReadOnly(True)
        shimTestStatText = QTextEdit()
        shimTestStatText.setReadOnly(True)
        self.shimStatText = [shimBackStatText, shimExpectStatText, shimTestStatText]
        statsLayout = QHBoxLayout()
        statsLayout.addWidget(shimBackStatText)
        statsLayout.addWidget(shimExpectStatText)
        statsLayout.addWidget(shimTestStatText)
        leftLayout.addWidget(shimStatTextLabel)
        leftLayout.addLayout(statsLayout)

        self.saveResultsButton = self.addButtonConnectedToFunction(leftLayout, "Save results", self.saveResults)

        # RIGHT SIDE

        # MANUAL SHIMMING OPERATIONS START
        # horizontal box to split up the calibrate zero and get current buttons from the manual set current button and entries 
        self.domanualShimLabel = QLabel(f"MANUAL SHIM OPERATIONS")
        rightLayout.addWidget(self.domanualShimLabel)
        manualShimLayout = QHBoxLayout()
        rightLayout.addLayout(manualShimLayout)

        calZeroGetcurrentLayout = QVBoxLayout()
        manualShimLayout.addLayout(calZeroGetcurrentLayout)

        # add the calibrate zero and get current buttons to left of manualShimLayout
        manualShimButtonsLayout = QVBoxLayout()
        manualShimLayout.addLayout(manualShimButtonsLayout)
        self.shimCalChannelsButton = self.addButtonConnectedToFunction(manualShimButtonsLayout, "Calibrate Shim Channels", self.shimInstance.shimCalibrate)
        self.shimZeroButton        = self.addButtonConnectedToFunction(manualShimButtonsLayout, "Zero Shim Channels", self.shimInstance.shimZero)
        self.shimGetCurrentsButton = self.addButtonConnectedToFunction(manualShimButtonsLayout, "Get Shim Currents", self.shimInstance.shimGetCurrent)

        # add the vertical region for channel input, current input, and set current button right of manualShimLayout
        setChannelCurrentShimLayout = QVBoxLayout()
        manualShimLayout.addLayout(setChannelCurrentShimLayout)
        self.shimManualChannelEntry = self.addEntryWithLabel(setChannelCurrentShimLayout, "Channel Index (Int): ", QIntValidator(0, self.shimInstance.numLoops-1))
        self.shimManualCurrenEntry = self.addEntryWithLabel(setChannelCurrentShimLayout, "Current (A): ", QDoubleValidator(-2.4, 2.4, 2))
        self.shimManualSetCurrentButton = self.addButtonConnectedToFunction(setChannelCurrentShimLayout, "Shim: Set Currents", self.shimInstance.shimSetCurrent)
        self.shimManualSetCurrentButton.setEnabled(False) # TODO: fix this / connect it idk
        self.slowButtons += [self.shimCalChannelsButton, self.shimZeroButton, self.shimGetCurrentsButton, self.shimManualSetCurrentButton]

        # ACTUAL SHIM OPERATIONS START
        # Just add the rest of the things to the vertical layout.
        # add a label and select for the slice index
        self.shimSliceIndexSlider, self.shimSliceIndexEntry = self.addLabeledSliderAndEntry(rightLayout, "Slice Index (Int): ", QIntValidator(0, 2147483647)) #TODO: validate this after somehow....
        self.shimSliceIndexSlider.valueChanged.connect(self.updateFromShimSliceIndexSlider)
        self.shimSliceIndexEntry.editingFinished.connect(self.updateFromShimSliceIndexEntry)
        self.shimSliceIndexSlider.setValue(self.shimSliceIndex)
        self.shimSliceIndexEntry.setText(str(self.shimSliceIndex))
        self.shimSliceIndexSlider.setEnabled(False)
        self.shimSliceIndexEntry.setEnabled(False)

        recomputeLayout = QHBoxLayout()
        self.recomputeCurrentsButton = self.addButtonConnectedToFunction(recomputeLayout, "Shim: Recompute Currents", self.recomputeCurrentsAndView)
        self.slowButtons += [self.recomputeCurrentsButton]
        self.currentsDisplay = QLineEdit()
        self.currentsDisplay.setReadOnly(True)
        recomputeLayout.addWidget(self.currentsDisplay)
        rightLayout.addLayout(recomputeLayout)

        self.doShimProcedureLabel = QLabel("SHIM OPERATIONS; Default linear gradient shims = _")
        rightLayout.addWidget(self.doShimProcedureLabel)

        self.shimDeltaTESlider, self.shimDeltaTEEntry = self.addLabeledSliderAndEntry(rightLayout, "Delta TE (us): ", QIntValidator(100, 500))
        self.shimDeltaTESlider.setMaximum(self.shimDeltaTE)
        self.shimDeltaTESlider.setMinimum(100)
        self.shimDeltaTESlider.setValue(self.shimDeltaTE)
        self.shimDeltaTEEntry.setText(str(self.shimDeltaTE))
        self.shimDeltaTESlider.valueChanged.connect(self.updateFromShimDeltaTESlider)
        self.shimDeltaTEEntry.editingFinished.connect(self.updateFromShimDeltaTEEntry)

        self.shimCalCurrentSlider, self.shimCalCurrentEntry = self.addLabeledSliderAndEntry(rightLayout, "Calibration Current (A): ", QDoubleValidator(.1, 2, 2))
        self.shimCalCurrentSlider.setMaximum(190)
        self.shimCalCurrentSlider.setMinimum(0)
        self.shimCalCurrentSlider.setValue(self.shimCalCurrent*100-10)
        self.shimCalCurrentEntry.setText(str(self.shimCalCurrent))
        self.shimCalCurrentSlider.valueChanged.connect(self.updateFromshimCalCurrentSlider)
        self.shimCalCurrentEntry.editingFinished.connect(self.updateFromshimCalCurrentEntry)

        self.slowButtons += [self.shimDeltaTESlider, self.shimDeltaTEEntry, self.shimCalCurrentSlider, self.shimCalCurrentEntry]
        # macro for obtaining background scans
        self.doBackgroundScansButton, self.doBackgroundScansMarker = self.addButtonWithFuncAndMarker(rightLayout, "Shim: Obtain Background B0map", self.doBackgroundScans)
        loopCalibrationLayout = QHBoxLayout()
        rightLayout.addLayout(loopCalibrationLayout)
        self.withLinGradMarker = QCheckBox("With Lin. Gradients")
        self.withLinGradMarker.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.withLinGradMarker.setEnabled(False)
        self.withLinGradMarker.setChecked(True)
        loopCalibrationLayout.addWidget(self.withLinGradMarker)
        self.doLoopCalibrationScansButton, self.doLoopCalibrationScansMarker = self.addButtonWithFuncAndMarker(loopCalibrationLayout, "Shim: Obtain Loop Basis B0maps", self.doLoopCalibrationScans)
        setAllCurrentsLayout = QHBoxLayout() # need a checkbox in front of the set all currents button to show that the currents have been computed
        rightLayout.addLayout(setAllCurrentsLayout)
        self.currentsComputedMarker = QCheckBox("Currents Computed?")
        self.currentsComputedMarker.setEnabled(False)
        self.currentsComputedMarker.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        setAllCurrentsLayout.addWidget(self.currentsComputedMarker)
        self.setAllCurrentsButton, self.setAllCurrentsMarker = self.addButtonWithFuncAndMarker(setAllCurrentsLayout, "Current Selected Slice: Set Optimal Currents", self.shimSetAllCurrents)
        self.doShimmedScansButton, self.doShimmedScansMarker = self.addButtonWithFuncAndMarker(rightLayout, "Current Selected Slice: Perform Shimmed Scan", self.doShimmedScans)
        self.doEvalApplShimsButton = self.addButtonConnectedToFunction(rightLayout, "Evaluate Applied Shims", self.doEvalAppliedShims)

        self.doAllShimmedScansButton, self.doAllShimmedScansMarker = self.addButtonWithFuncAndMarker(rightLayout, "Shimmed Scan ALL Slices", self.doAllShimmedScans)
        self.slowButtons += [self.doBackgroundScansButton, self.doLoopCalibrationScansButton, self.setAllCurrentsButton, self.doShimmedScansButton, self.doEvalApplShimsButton, self.doAllShimmedScansButton]

        # Add the log output here
        self.shimLogOutput = QTextEdit()
        self.shimLogOutput.setReadOnly(True)
        self.shimLogOutputLabel = QLabel("SHIM Log Output")
        rightLayout.addWidget(self.shimLogOutputLabel)
        rightLayout.addWidget(self.shimLogOutput)
    
    def setup3rdTabLayout(self, layout):
        """add two panes, left for visualizing each basis function, via a slider, right for visualizing the histograms of shimmed results and such"""
        hlayout = QHBoxLayout()
        layout.addLayout(hlayout)

        # LEFT
        leftLayout = QVBoxLayout()
        hlayout.addLayout(leftLayout)

        # add a graphics view for visualizing the basis function
        self.basicViewLabel = QLabel()
        self.basisView = ImageViewer(self, self.basicViewLabel)
        leftLayout.addWidget(self.basisView, alignment=Qt.AlignmentFlag.AlignCenter)
        leftLayout.addWidget(self.basicViewLabel)
        self.basisView.setFixedSize(512, 512)  # Set a fixed size for the view
        self.basisView.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)

        # add a slider for selecting the basis function
        self.basisFunctionSlider, self.basisFunctionEntry = self.addLabeledSliderAndEntry(leftLayout, "Basis Function Index (Int): ", QIntValidator(0, self.shimInstance.numLoops + 3 - 1))
        self.basisFunctionEntry.editingFinished.connect(self.updateFromBasisFunctionEntry)
        self.basisFunctionSlider.valueChanged.connect(self.updateFromBasisFunctionSlider)
        self.basisFunctionSlider.setValue(self.basisFunctionIndex)
        self.basisFunctionEntry.setText(str(self.basisFunctionIndex))

        # add a label and select for the slice index
        self.basisSliceIndexSlider, self.basisSliceIndexEntry = self.addLabeledSliderAndEntry(leftLayout, "Slice Index (Int): ", QIntValidator(0, 2147483647)) #TODO: validate this after
        self.basisSliceIndexSlider.valueChanged.connect(self.updateFromBasisSliceIndexSlider)
        self.basisSliceIndexEntry.editingFinished.connect(self.updateFromBasisSliceIndexEntry)
        self.basisSliceIndexSlider.setValue(self.basisSliceIndex)
        self.basisSliceIndexEntry.setText(str(self.basisSliceIndex))
        # RIGHT
        #TODO later



    def addButtonConnectedToFunction(self, layout, buttonName, function):
        button = QPushButton(buttonName)
        button.clicked.connect(function)
        layout.addWidget(button)
        return button

    def addEntryWithLabel(self, layout, labelStr, entryvalidator):
        label = QLabel(labelStr)
        entry = QLineEdit()
        entry.setValidator(entryvalidator)
        labelEntryLayout = QHBoxLayout()
        labelEntryLayout.addWidget(label)
        labelEntryLayout.addWidget(entry)
        layout.addLayout(labelEntryLayout)
        return entry

    def addLabeledSlider(self, layout, labelStr, orientation=Qt.Orientation.Horizontal):
        slider = QSlider(orientation)
        label = QLabel(labelStr)
        labelEntryLayout = QHBoxLayout()
        labelEntryLayout.addWidget(label)
        labelEntryLayout.addWidget(slider)
        slider.setMinimum(0)
        slider.setMaximum(self.roiSliderGranularity)
        slider.setValue((round(self.roiSliderGranularity)//2))
        layout.addLayout(labelEntryLayout)
        return slider

    def addLabeledSliderAndEntry(self, layout, labelStr, entryvalidator):
        slider = QSlider(Qt.Orientation.Horizontal)
        label = QLabel(labelStr)
        entry = QLineEdit()
        entry.setValidator(entryvalidator)
        labelEntryLayout = QHBoxLayout()
        labelEntryLayout.addWidget(label)
        labelEntryLayout.addWidget(slider)
        labelEntryLayout.addWidget(entry)
        layout.addLayout(labelEntryLayout)
        return slider, entry
    
    def addButtonWithFuncAndMarker(self, layout, buttonName, function, markerName="Done?"):
        hlayout = QHBoxLayout()
        layout.addLayout(hlayout)
        marker = QCheckBox(markerName)
        marker.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if markerName == "Done?":
            marker.setEnabled(False)
            button = self.addButtonConnectedToFunction(hlayout, buttonName, function)
            hlayout.addWidget(marker)
        else:
            hlayout.addWidget(marker)
            button = self.addButtonConnectedToFunction(hlayout, buttonName, function)
        return button, marker

    ##### GRAPHICS FUNCTION DEFINITIONS #####   

    def setViewImage(self, qImage, roi=True):
        pixmap = QPixmap.fromImage(qImage)
        if roi:
            self.roiView.viewport().setVisible(True)
            self.roiPixmapItem.setPixmap(pixmap)
            self.roiScene.setSceneRect(self.roiPixmapItem.boundingRect())  # Adjust scene size to the pixmap's bounding rect
            self.roiView.fitInView(self.roiPixmapItem, Qt.AspectRatioMode.KeepAspectRatio)  # Fit the view to the item
            self.roiPlaceholderText.setVisible(False)
            self.roiView.viewport().update()  # Force the viewport to update
        else:
            self.shimView.viewport().setVisible(True)
            self.shimView.set_pixmap(pixmap)
            self.shimView.setSceneRect(self.shimView.pixmap_item.boundingRect())  # Adjust scene size to the pixmap's bounding rect
            self.shimView.fitInView(self.shimView.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)  # Fit the view to the item
            self.shimView.viewport().update()  # Force the viewport to update
    
    def updateROIImageDisplay(self):
        if self.currentROIImageData is not None:
            self.validateROISliceIndexControls(self.roiSliceIndex)
            # Extract the slice and normalize it
            if self.roiVizButtonGroup.checkedId() == 1:
                sliceData = np.ascontiguousarray(self.currentROIImageData[:,self.roiSliceIndex,:]).astype(float)
            else:
                sliceData = np.ascontiguousarray(self.currentROIImageData[self.roiSliceIndex]).astype(float)
            normalizedData = (sliceData - sliceData.min()) / (sliceData.max() - sliceData.min()) * 255
            displayData = normalizedData.astype(np.uint8)  # Convert to uint8
            # Create a 3-channel image from grayscale data
            rgbData = np.stack((displayData,)*3, axis=-1)
            height, width, _ = rgbData.shape
            bytesPerLine = rgbData.strides[0] 
            self.roiQImage = QImage(rgbData.data, width, height, bytesPerLine, QImage.Format.Format_RGB888)
            if self.roiQImage.isNull():
                self.log("Debug: Failed to create QImage")
            else:
                if self.roiEditorEnabled:
                    self.visualizeROI()
                else:
                    self.setViewImage(self.roiQImage)
        else:
            self.roiPlaceholderText.setVisible(True)

    def validateROISliceIndexControls(self, value):
        if self.currentROIImageData is not None:
            # Update the slider range based on the new data
            if self.roiVizButtonGroup.checkedId() == 1:
                self.roiDepth = self.currentROIImageData.shape[1]
            else:
                self.roiDepth = self.currentROIImageData.shape[0]
            self.roiSliceIndexSlider.setMinimum(0)
            self.roiSliceIndexSlider.setMaximum(self.roiDepth - 1)
            self.roiSliceIndexEntry.setValidator(QIntValidator(0, self.roiDepth - 1))

            # If roiSliceIndex is None or out of new bounds, default to first slice
            if value is None:
                self.log("DEBUG: Invalid slice index, defaulting to 0")
                self.roiSliceIndex = 0
            elif value >= self.roiDepth:
                self.log("DEBUG: Invalid slice index, defaulting to roi.depth - 1")
                self.roiSliceIndex = self.roiDepth - 1
            else:
                self.roiSliceIndex = value
        else:
            self.roiSliceIndex = value
    
    def visualizeROI(self):
        # TODO add mote kinds of shapes and make this more scalable 

        # if a slider value has changed, set self.newROI
        for i in range(3):
            if self.roiSizeSliders[i].value() != self.sizes[i] or self.roiPositionSliders[i].value() != self.center[i]:
                self.newROI = True
            self.sizes[i] = self.roiSizeSliders[i].value() / 100
            self.center[i] = self.roiPositionSliders[i].value() / 100
        
        self.XSizeEllipsoid = round((self.roiQImage.width() // 2) * self.sizes[0])
        self.YSizeEllipsoid = round((self.roiQImage.height() // 2) * self.sizes[1])
        self.ZSizeEllipsoid = round((self.roiDepth // 2) * self.sizes[2])
        self.XCenterEllipsoid = round(self.roiQImage.width() * self.center[0])
        self.YCenterEllipsoid = round(self.roiQImage.height() * self.center[1])
        self.ZCenterEllipsoid = round(self.roiDepth * self.center[2])

        # self.log(f"DEBUG: Drawing oval on slice {self.roiSliceIndex}")
        # self.log(f"DEBUG: XCenter: {self.XCenterEllipsoid}, YCenter: {self.YCenterEllipsoid}, ZCenter: {self.ZCenterEllipsoid}")
        # self.log(f"DEBUG: XSize: {self.XSizeEllipsoid}, YSize: {self.YSizeEllipsoid}, ZSize: {self.ZSizeEllipsoid}")

        # based on self.roiSliceIndex, we can determine what percent of SizeEllipsoid to use for the oval slice
        offsetFromDepthCenter = abs(self.roiSliceIndex - self.ZCenterEllipsoid)
        # self.log(f"DEBUG: Offset from depth center: {offsetFromDepthCenter}")
        if offsetFromDepthCenter <= self.ZSizeEllipsoid:
            factor = (1 - (offsetFromDepthCenter**2 / self.ZSizeEllipsoid**2))
            # self.log(f"DEBUG: Factor for oval: {factor}")
            width_oval = round(np.sqrt(self.XSizeEllipsoid**2 * factor))
            height_oval = round(np.sqrt(self.YSizeEllipsoid**2 * factor))
            # self.log(f"DEBUG: Width: {width_oval}, Height: {height_oval}")

            # visualize an oval overlayed on top of the current self.roiQImage
            # Create a QPainter object and begin painting on the QImage
            qImage = self.roiQImage.copy()
            painter = QPainter(qImage)
            # Set the pen color to red and the brush to a semi transparent red
            painter.setPen(QPen(QBrush(QColor(255, 0, 0, 100)), 1))

            points = []
            for y in range(self.shimImages[0].shape[0]):
                for x in range(self.shimImages[0].shape[2]):
                    if ((x - self.XCenterEllipsoid)**2 / self.XSizeEllipsoid**2 +
                        (y - self.YCenterEllipsoid)**2 / self.YSizeEllipsoid**2 +
                        (self.roiSliceIndex - self.ZCenterEllipsoid)**2 / self.ZSizeEllipsoid**2) <= 1:
                        # If it is, set the corresponding element in the mask to True
                        points.append((x, y))

            for x, y in points:
                painter.drawPoint(x, y)
            # Draw an oval on the image. Adjust the parameters as needed for your specific use case
            # painter.drawEllipse(QPoint(self.XCenterEllipsoid, self.YCenterEllipsoid), width_oval, height_oval)
            # End painting
            painter.end()

            # Now you can use the QImage as before
            self.setViewImage(qImage)
        else:
            self.setViewImage(self.roiQImage)

    def updateFromSliceROIEntry(self):
        # Update the display based on the manual entry in QLineEdit
        index = int(self.roiSliceIndexEntry.text()) if self.roiSliceIndexEntry.text() else 0
        self.validateROISliceIndexControls(index)
        self.roiSliceIndexSlider.setValue(self.roiSliceIndex)
        self.updateROIImageDisplay()

    def updateFromROISliceSlider(self, value):
        # Directly update the line edit when the slider value changes
        self.validateROISliceIndexControls(value)
        self.roiSliceIndexEntry.setText(str(self.roiSliceIndex))
        self.updateROIImageDisplay()

    # TODO should make this a single function since it is basically a copy of the other one
    def updateShimImageAndStats(self):
        select = self.shimVizButtonGroup.checkedId()
        self.shimImage = self.shimImages[select]
        if self.shimImage is not None:
            self.shimImage = self.shimImage if select == 0 else self.shimImage[self.shimSliceIndex]
            if self.shimImage is not None:
                sliceData = np.ascontiguousarray(self.shimImage[:,self.shimSliceIndex,:]).astype(float)
                # want this normalization to be constant for every slice and for every kind of image...
                normalizedData = (sliceData - self.shimImageValMin) / (self.shimImageValMax - self.shimImageValMin) * 255
                displayData = normalizedData.astype(np.uint8)  # Convert to uint8
                height, width = displayData.shape
                bytesPerLine = displayData.strides[0] 
                qImage = QImage(displayData.data, width, height, bytesPerLine, QImage.Format.Format_Grayscale8)
                if qImage.isNull():
                    self.log("Debug: Failed to create QImage")
                else:
                    self.shimView.viewport().setVisible(True)
                    pixmap = QPixmap.fromImage(qImage)
                    self.shimView.set_pixmap(pixmap)
                    self.shimView.setSceneRect(self.shimView.pixmap_item.boundingRect())  # Adjust scene size to the pixmap's bounding rect
                    self.shimView.fitInView(self.shimView.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)  # Fit the view to the item
                    self.shimView.viewport().update()  # Force the viewport to update

                # show as many stats as are available for the specific slice
                prefixs = ["Background ", "Est. ", "Actual "]
                for i in range(3):
                    text = "\nNo stats available"                
                    if self.shimStatStrs[i] is not None:
                        stats = self.shimStatStrs[i][self.shimSliceIndex]
                        if stats is not None:
                            text = stats
                    text = prefixs[i] + text
                    self.shimStatText[i].setText(text)

                # if original gradient shim values obtained:
                shimtxt = ""
                if self.linShims is not None:
                    shimtxt += f"Default linear gradient shims = {self.linShims}"
                if self.exsiInstance.ogCenterFreq is not None:
                    shimtxt += f"OG CF = {self.exsiInstance.ogCenterFreq} Hz"
                self.doShimProcedureLabel.setText(f"SHIM OPERATIONS; " + shimtxt)
                # if currents are available
                if self.currents is not None:
                    if self.currents[self.shimSliceIndex] is not None:
                        text = f"Δcf:{int(round(self.currents[self.shimSliceIndex][0]))}|"
                        numIter = self.shimInstance.numLoops
                        if self.withLinGradMarker.isChecked():
                            numIter += 3
                        pref = ["X", "Y", "Z"]
                        for i in range(numIter):
                            if self.withLinGradMarker.isChecked():
                                if i < 3:
                                    text += 'gStep' + pref[i]
                                    text += f":{self.currents[self.shimSliceIndex][i+1]*self.linShimFactor:.0f}|"
                                else:
                                    text += f"ch{i-3}:{self.currents[self.shimSliceIndex][i+1]:.2f}|"
                            else:
                                text += f"ch{i}:{self.currents[self.shimSliceIndex][i+1]:.2f}|"
                    else:
                        text = "No currents available "
                    self.currentsDisplay.setText(text[:-1])
                else:
                    self.currentsDisplay.setText("No currents available")
                return 
        self.shimView.viewport().setVisible(False)
    
    # TODO: remove the "value" since it is always passing self.shimSliceIndex
    def validateShimSliceIndexControls(self, value):
        if self.shimImages[0] is not None:
            # update depth to consider which orientation is selected. in which case it wont be 0 here if not coronal
            depth = self.shimImages[0].shape[1]
            self.shimSliceIndexEntry.setValidator(QIntValidator(0, depth - 1))
            self.shimSliceIndexSlider.setMinimum(0)
            self.shimSliceIndexSlider.setMaximum(depth - 1)
            # If shimSliceIndex is None or out of new bounds, default to first slice
            if value is None or value >= depth:
                self.log("DEBUG: Invalid slice index, defaulting to 0")
                self.shimSliceIndex = 0
            else:
                self.shimSliceIndex = value

        self.shimImageValMax, self.shimImageValMin = -np.inf, np.inf
        for i in range(3):
            if self.shimImages[i] is not None:
                if i == 0:
                    self.shimImageValMax = max(np.nanmax(np.abs(self.shimImages[i])), self.shimImageValMax)
                else:
                    for j in range(self.shimImages[0].shape[1]):
                        if self.shimImages[i][j] is not None:
                            nanmax = np.nanmax(np.abs(self.shimImages[i][j]))
                            if not np.isnan(nanmax):
                                self.shimImageValMax = max(nanmax, self.shimImageValMax)
        # this should make sure that 0 Hz offset is always in the center of the color bar / spectrum
        self.shimImageValMin = -self.shimImageValMax

    def updateFromShimSliceIndexEntry(self):
        index = int(self.shimSliceIndexEntry.text()) if self.shimSliceIndexEntry.text() else 0
        self.shimSliceIndexSlider.setValue(index)
        self.shimSliceIndex = index
        self.setShimImages()
        self.updateShimImageAndStats()

    def updateFromShimSliceIndexSlider(self, value):
        # Directly update the line edit when the slider value changes
        self.shimSliceIndex = value
        self.shimSliceIndexEntry.setText(str(self.shimSliceIndex))
        self.setShimImages()
        self.updateShimImageAndStats()

    def updateFromShimDeltaTEEntry(self):
        value = int(self.shimDeltaTEEntry.text()) if self.shimDeltaTEEntry.text() else 0
        self.shimDeltaTESlider.setValue(value)
        self.shimDeltaTE = value

    def updateFromShimDeltaTESlider(self, value):
        self.shimDeltaTE = value
        self.shimDeltaTEEntry.setText(str(self.shimDeltaTE))
    
    def updateFromshimCalCurrentEntry(self):
        value = float(self.shimCalCurrentEntry.text()) if self.shimCalCurrentEntry.text() else 0
        self.shimCalCurrent = value
        self.shimCalCurrentSlider.setValue(int(round(value*100))-10)
     
    def updateFromshimCalCurrentSlider(self, value):
        value = (value + 10) / 100
        self.shimCalCurrent = value
        self.shimCalCurrentEntry.setText(str(self.shimCalCurrent))
    
    def updateBasisView(self):
        if self.basisImages is not None:
            # Extract the slice and normalize it
            sliceData = np.ascontiguousarray(self.basisImages[self.basisFunctionIndex][self.basisSliceIndex]).astype(float)
            self.lastSliceData = sliceData
            normalizedData = (sliceData - self.basisMin) / (self.basisMax - self.basisMin)* 255
            displayData = normalizedData.astype(np.uint8)  # Convert to uint8
            # np.savetxt(f"basis{self.basisFunctionIndex}_{self.basisSliceIndex}.csv", displayData, delimiter=",")
            height, width = displayData.shape
            bytesPerLine = displayData.strides[0] 
            qImage = QImage(displayData.data, width, height, bytesPerLine, QImage.Format.Format_Grayscale8)
            if qImage.isNull():
                self.log("Debug: Failed to create QImage")
            else:
                self.basisView.viewport().setVisible(True)
                pixmap = QPixmap.fromImage(qImage)
                self.basisView.set_pixmap(pixmap)
                self.basisView.pixmap_item.setCacheMode(QGraphicsItem.CacheMode.NoCache)
                self.basisView.scene.setSceneRect(self.basisView.pixmap_item.boundingRect())  # Adjust scene size to the pixmap's bounding rect
                self.basisView.fitInView(self.basisView.pixmap_item, Qt.AspectRatioMode.KeepAspectRatio)  # Fit the view to the item
                self.basisView.viewport().repaint()

    def updateFromBasisFunctionEntry(self):
        index = int(self.basisFunctionEntry.text()) if self.basisFunctionEntry.text() else 0
        self.log(f"DEBUG: Updating basis function entry to {index}")
        self.basisFunctionSlider.setValue(index)
        self.basisFunctionIndex = index
        self.updateBasisView()
    
    def updateFromBasisFunctionSlider(self, value):
        self.log(f"DEBUG: Updating basis function slider to {value}")
        self.basisFunctionIndex = value
        self.basisFunctionEntry.setText(str(self.basisFunctionIndex))
        self.updateBasisView()
    
    def updateFromBasisSliceIndexEntry(self):
        index = int(self.basisSliceIndexEntry.text()) if self.basisSliceIndexEntry.text() else 0
        self.log(f"DEBUG: Updating basis slice index slider to {index}")
        self.basisSliceIndexSlider.setValue(index)
        self.basisSliceIndex = index
        self.updateBasisView()
    
    def updateFromBasisSliceIndexSlider(self, value):
        self.log(f"DEBUG: Updating basis slice index slider to {value}")
        self.basisSliceIndex = value
        self.basisSliceIndexEntry.setText(str(self.basisSliceIndex))
        self.updateBasisView()
    
    def updateExsiLogOutput(self, text):
        self.exsiLogOutput.append(text)

    def updateShimLogOutput(self, text):
        self.shimLogOutput.append(text)

    def toggleShimImage(self, id):
        self.log(f"DEBUG: Toggling shim image to {id}")
        self.shimImage = self.shimImages[id]
        self.updateShimImageAndStats()
    
    def toggleROIBackgroundImage(self):
        if self.roiVizButtonGroup.checkedId() == 1 and self.doBackgroundScansMarker.isChecked():
            self.renderLatestDataButton.setEnabled(True)

    def toggleROIEditor(self):
        if self.roiVizButtonGroup.checkedId() == 1:
            if self.roiEditorEnabled:
                self.roiEditorEnabled = False
                self.roiToggleButton.setText("Enable ROI Editor")
            else:
                for i in range(3):
                    self.roiSizeSliders[i].setEnabled(True)
                    self.roiPositionSliders[i].setEnabled(True)
                self.roiEditorEnabled = True
                self.roiToggleButton.setText("Disable ROI Editor")
            self.updateROIImageDisplay()

    def onTabSwitch(self, index):
        self.log(f"DEBUG: Switched to tab {index}")
        if index == 1:
            if self.newROI:
                self.saveROIMask()
                self.recomputeCurrentsAndView()
            self.newROI = False
        if index == 2:
            self.setBasisImages()
            self.updateBasisView()
                        
    
    ##### HELPER FUNCTIONS FOR EXSI CONTROL BUTTONS !!!! NO BUTTON SHOULD MAP TO THIS #####
    
    def queueBasisPairScanDetails(self, linGrad=None):
        """
        once the b0map sequence is loaded, subroutines are iterated along with cvs to obtain basis maps.
        linGrad should be a list of 3 floats if it is not None
        """
        # TODO: eventually add these to the config file
        cvs = {"act_tr": 3300, "act_te": [1104, 1604], "rhrcctrl": 13, "rhimsize": 64}
        for i in range(2):
            self.exsiInstance.sendSelTask()
            self.exsiInstance.sendActTask()
            if linGrad:
                self.exsiInstance.sendSetShimValues(*linGrad)
                self.exsiInstance.sendSetCenterFrequency(int(self.exsiInstance.ogCenterFreq))
            for cv in cvs.keys():
                if cv == "act_te":
                    if i == 0:
                        self.exsiInstance.sendSetCV(cv, cvs[cv][0])
                    else:
                        self.exsiInstance.sendSetCV(cv, cvs[cv][0] + self.shimDeltaTE)
                else:
                    self.exsiInstance.sendSetCV(cv, cvs[cv])
            self.exsiInstance.sendPatientTable()
            if not self.autoPrescanDone:
                self.exsiInstance.sendPrescan(auto=True)
                self.autoPrescanDone = True
            else:
                self.exsiInstance.sendPrescan(auto=False)
            self.exsiInstance.sendScan()

    def queueBasisPairScan(self, linGrad=None):
        # Basic basis pair scan. should be used to scan the background
        self.exsiInstance.sendLoadProtocol("ConformalShimCalibration3")
        self.queueBasisPairScanDetails(linGrad)

    def queueCaliBasisPairScan(self, channelNum):
        # when the exsiclient gets this specific command, it will know to dispatch both the loadProtocol 
        # command and also a Zero Current and setCurrent to channelNum with calibration current of 1.0
        self.exsiInstance.sendLoadProtocol(f"ConformalShimCalibration3 | {channelNum} {self.shimCalCurrent}")
        self.queueBasisPairScanDetails()
        self.log(f"DEBUG: DONE queueing basis paid scan!")

    def shimSetCurrentManual(self, channel, current, board=0):
        """helper function to set the current for a specific channel on a specific board."""
        if self.shimInstance:
            self.shimInstance.send(f"X {board} {channel} {current}")

    def countScansCompleted(self, n):
        """should be 2 for every basis pair scan"""
        for i in range(n):
            self.log(f"DEBUG: checking to see if failure happened on last run: currently on scan {i+1} / {n}")
            if not self.exsiInstance.no_failures.is_set():
                self.log("Error: scan failed")
                self.exsiInstance.no_failures.set()
                return False
            self.log(f"DEBUG: Waiting for scan to complete, currently on scan {i+1} / {n}")
            if not self.exsiInstance.images_ready_event.wait(timeout=90):
                self.log(f"Error: scan {i+1} / {n} didn't complete within 90 seconds bruh")
                return False
            else:
                self.exsiInstance.images_ready_event.clear()
                # TODO probably should raise some sorta error here...
        self.log(f"DEBUG: {n} scans completed!")
        # after scans get completed, go ahead and get the latest scan data over on this machine...
        self.transferScanData()
        return True

    def triggerComputeShimCurrents(self):
        """if background and basis maps are obtained, compute the shim currents"""
        if self.doBackgroundScansMarker.isChecked() and self.doLoopCalibrationScansMarker.isChecked():
            self.shimData[1] = None
            self.computeShimCurrents()
        elif self.doBackgroundScansMarker.isChecked():
            self.computeMask()
        self.evaluateShimImages()
    
    def saveROIMask(self):
        # make a mask the same shape as self.shimImages[0] based on the ellipsoid parameters
        if self.roiEditorEnabled:
            self.log(f"DEBUG: Saving ROI mask")
            mask = np.zeros_like(self.shimImages[0], dtype=bool)
            for z in range(mask.shape[1]):
                for y in range(mask.shape[0]):
                    for x in range(mask.shape[2]):
                        if ((x - self.XCenterEllipsoid)**2 / self.XSizeEllipsoid**2 +
                            (y - self.YCenterEllipsoid)**2 / self.YSizeEllipsoid**2 +
                            (z - self.ZCenterEllipsoid)**2 / self.ZSizeEllipsoid**2) <= 1:
                            # If it is, set the corresponding element in the mask to True
                            mask[y, z, x] = True
            self.roiMask = mask
            # apply the mask to any of the shimImages that may exist
        else:
            self.roiMask = None
        self.computeMask()
        self.setShimImages()

    def setShimImages(self):
        """apply the mask to the shimImages and set the shimImage to the correct slice index."""
        if self.shimData[0] is not None:
            if self.finalMask is not None and self.finalMask[self.shimSliceIndex] is not None:
                #TODO: make shimImages[0] also 2d mtx, and not have it so dynamic that this function needs to be run every time...
                self.shimImages[0] = np.where(self.finalMask[self.shimSliceIndex], self.shimData[0], np.nan)
            else:
                self.shimImages[0] = self.shimData[0]
        for i in range(1,3):
            if self.shimData[i] is not None:
                self.shimImages[i] = [None] * self.shimImages[0].shape[1]
                #TODO: finalMask being one 3d mtx for every slice index is pretty dumb ngl... logic could be simplified and a lot of loops saved...
                for j in range(self.shimImages[0].shape[i]):
                    if self.shimData[i][j] is not None and self.finalMask is not None and self.finalMask[j] is not None:
                        self.shimImages[i][j] = np.where(self.finalMask[j], self.shimData[i][j], np.nan)
                    else:
                        self.shimImages[i][j] = np.where(np.zeros_like(self.shimImages[0], dtype=bool), np.nan, np.nan)
    
    def setBasisImages(self):
        #if not self.basisImagesSet and self.finalMask is not None and self.basisB0maps is not None:
        if True:
            self.basisMin = np.inf
            self.basisMax = -np.inf
            self.basisImages = [None] * (self.shimInstance.numLoops + 3)
           
            for i in range(len(self.basisImages)):
                self.basisImages[i] = np.zeros_like(self.basisB0maps[0]).transpose(1,0,2)
                # self.basisImages[i] = np.zeros((64,60,64))
                for j in range(self.basisB0maps[0].shape[1]):
                # for j in range(64):
                    if self.finalMask[j] is not None:
                        self.basisImages[i][j] = np.where(self.finalMask[j], self.basisB0maps[i], np.nan)[:,j,:]
                    else:
                        self.basisImages[i][j] = self.basisB0maps[i][:,j,:]
                    # self.basisImages[i][j] = np.random.random((60,64))
                self.basisMin = np.nanmin([np.nanmin(self.basisImages[i]), self.basisMin])
                self.basisMax = np.nanmax([np.nanmax(self.basisImages[i]), self.basisMax])
            self.basisFunctionSlider.setMaximum(len(self.basisImages)-1)
            self.basisFunctionEntry.setValidator(QIntValidator(0, len(self.basisImages)-1))
            self.basisSliceIndexSlider.setMaximum(self.basisImages[0].shape[0]-1)
            self.basisSliceIndexEntry.setValidator(QIntValidator(0, self.basisImages[0].shape[0]-1))
            self.basisImagesSet = True

    ##### BUTTON FUNCTION DEFINITIONS; These are the functions that handle button click #####   
    # as such they should all be decorated in some fashion to not allow for operations to happen if they cannot

    #### More macro type button functions. These are slow, so disable other buttons while they are running, but unblock the rest of the gui ####
    
    @disableSlowButtonsTillDone
    def getAndSetROIImageWork(self, trigger):
        if self.roiVizButtonGroup.checkedId() == 1:
            self.log("Debug: Getting background image") 
            self.getROIBackgound()
        else:
            self.log("Debug: Getting latest image") 
            self.transferScanData()
            if os.path.exists(self.localExamRootDir):
                self.getLatestData(stride=1)
            else:
                self.log("Debug: local directory has not been made yet...")
        trigger.finished.emit()
    @requireExsiConnection
    def doGetAndSetROIImage(self):
        work = Trigger()
        work.finished.connect(self.updateROIImageDisplay)
        kickoff_thread(self.getAndSetROIImageWork, args=(work,))


    @disableSlowButtonsTillDone
    def calibrationScanWork(self, trigger):
        self.exsiInstance.sendLoadProtocol("ConformalShimCalibration4")
        self.exsiInstance.sendSelTask()
        self.exsiInstance.sendActTask()
        self.exsiInstance.sendPatientTable()
        self.exsiInstance.sendScan()
        if self.exsiInstance.images_ready_event.wait(timeout=120):
            self.assetCalibrationDone = True
            self.exsiInstance.images_ready_event.clear()
        trigger.finished.emit()
    @requireExsiConnection
    def doCalibrationScan(self):
        # dont need to do the assetCalibration scan more than once
        if not self.exsiInstance or self.assetCalibrationDone:
            return
        trigger = Trigger()
        trigger.finished.connect(self.updateROIImageDisplay)
        kickoff_thread(self.calibrationScanWork, args=(trigger,))
    

    @disableSlowButtonsTillDone
    def fgreScanWork(self, trigger):
        self.exsiInstance.sendLoadProtocol("ConformalShimCalibration5")
        self.exsiInstance.sendSelTask()
        self.exsiInstance.sendActTask()
        self.exsiInstance.sendPatientTable()
        self.exsiInstance.sendScan()
        if not self.exsiInstance.images_ready_event.wait(timeout=120):
            self.log(f"Debug: scan didn't complete")
        else:
            self.exsiInstance.images_ready_event.clear()
            self.transferScanData()
            self.getLatestData(stride=1)
        trigger.finished.emit()
    @requireExsiConnection
    @requireAssetCalibration
    def doFgreScan(self):
        if not self.exsiInstance:
            return
        trigger = Trigger()
        trigger.finished.connect(self.updateROIImageDisplay)
        kickoff_thread(self.fgreScanWork, args=(trigger,))
    
    @disableSlowButtonsTillDone
    def recomputeCurrentsAndView(self):
        self.triggerComputeShimCurrents()
        self.updateShimImageAndStats()

    @disableSlowButtonsTillDone
    def waitBackgroundScan(self, trigger):
        self.doBackgroundScansMarker.setChecked(False)
        self.shimInstance.shimZero() # NOTE(rob): Hopefully this zeros quicker that the scans get set up...
        self.shimData[0] = None
        self.exsiInstance.images_ready_event.clear()
        if self.countScansCompleted(2):
            self.roiVizButtonGroup.buttons()[1].setEnabled(True)
            self.transferScanData()
            self.log("DEBUG: just finished all the background scans")
            self.computeBackgroundB0map()
            # if this is a new background scan and basis maps were obtained, then compute the shim currents
            self.doBackgroundScansMarker.setChecked(True)
            self.exsiInstance.send("GetPrescanValues") # get the center frequency
            self.triggerComputeShimCurrents()
            self.setShimImages()
            self.validateShimSliceIndexControls(self.shimSliceIndex) # to re compute scaling
            self.getLastSuccessgradients()
        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        trigger.finished.emit()
    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doBackgroundScans(self):
        # Perform the background scans for the shim system.
        trigger = Trigger()
        def updateVals():
            self.shimSliceIndexSlider.setEnabled(True)
            self.shimSliceIndexEntry.setEnabled(True)
            self.updateShimImageAndStats()
        trigger.finished.connect(updateVals)
        kickoff_thread(self.waitBackgroundScan, args=(trigger,))
        self.queueBasisPairScan()

    @disableSlowButtonsTillDone
    def waitLoopCalibrtationScan(self, trigger):
        self.doLoopCalibrationScansMarker.setChecked(False)
        self.shimInstance.shimZero() # NOTE(rob): Hopefully this zeros quicker that the scans get set up...
        self.rawBasisB0maps = None
        self.exsiInstance.images_ready_event.clear()
        num_scans = (self.shimInstance.numLoops + (3 if self.withLinGradMarker.isChecked() else 0)) * 2
        if self.countScansCompleted(num_scans):
            self.log("DEBUG: just finished all the calibration scans")
            self.computeBasisB0maps()
            # if this is a new background scan and basis maps were obtained, then compute the shim currents
            self.doLoopCalibrationScansMarker.setChecked(True)
            self.triggerComputeShimCurrents()
            self.setShimImages()
            self.validateShimSliceIndexControls(self.shimSliceIndex) # to recompute scaling
        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        trigger.finished.emit()
    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doLoopCalibrationScans(self):
        """Perform all the calibration scans for each loop in the shim system."""
        trigger = Trigger()
        trigger.finished.connect(self.updateShimImageAndStats)
        kickoff_thread(self.waitLoopCalibrtationScan, args=(trigger,))
        def queueAll():
            # perform the calibration scans for the linear gradients
            if self.withLinGradMarker.isChecked():
                self.queueBasisPairScan([20,0,0])
                self.queueBasisPairScan([0,20,0])
                self.queueBasisPairScan([0,0,20])
            for i in range(self.shimInstance.numLoops):
                self.queueCaliBasisPairScan(i)
        kickoff_thread(queueAll)


    @requireShimConnection
    def shimSetAllCurrents(self):
        if not self.currentsComputedMarker.isChecked() or not self.currents:
            self.log("Debug: Need to perform background and loop calibration scans before setting currents.")
            msg = createMessageBox("Error: Background And Loop Cal Scans not Done",
                                   "Need to perform background and loop calibration scans before setting currents.", 
                                   "You could set them manually if you wish to.")
            msg.exec() 
            return # do nothing more
        self.setAllCurrentsMarker.setChecked(False)
        if self.currents[self.shimSliceIndex] is not None:
            # setting center frequency
            self.log(f"DEBUG: Setting center frequency from {self.exsiInstance.ogCenterFreq} to {int(self.exsiInstance.ogCenterFreq) + int(round(self.currents[self.shimSliceIndex][0]))}")
            self.exsiInstance.sendSetCenterFrequency(int(self.exsiInstance.ogCenterFreq) + int(round(self.currents[self.shimSliceIndex][0])))

            # setting the linear shims
            if self.withLinGradMarker.isChecked():
                linGrads = self.linShimFactor * self.currents[self.shimSliceIndex][1:4]
                linGrads = np.round(linGrads).astype(int)
                self.exsiInstance.sendSetShimValues(*linGrads)
                
            # setting the loop shim currents
            for i in range(self.shimInstance.numLoops):
                if self.withLinGradMarker.isChecked():
                    self.log(f"DEBUG: Setting currents for loop {i} to {self.currents[self.shimSliceIndex][i+4]:.3f}, multiplied by {self.shimCalCurrent}")
                    self.shimSetCurrentManual(i%8, self.shimCalCurrent * self.currents[self.shimSliceIndex][i+4], i//8)
                else:
                    self.log(f"DEBUG: Setting currents for loop {i} to {self.currents[self.shimSliceIndex][i+1]:.3f}, multiplied by {self.shimCalCurrent}")
                    self.shimSetCurrentManual(i%8, self.shimCalCurrent * self.currents[self.shimSliceIndex][i+1], i//8)
        self.setAllCurrentsMarker.setChecked(True)


    @disableSlowButtonsTillDone
    def waitShimmedScans(self, trigger):
        self.doShimmedScansMarker.setChecked(False)
        self.shimData[2] = None
        self.exsiInstance.images_ready_event.clear()
        if self.countScansCompleted(2):
            self.computeShimmedB0Map()
            self.evaluateShimImages()
            self.setShimImages()
            self.doShimmedScansMarker.setChecked(True)
            self.validateShimSliceIndexControls(self.shimSliceIndex)
        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        trigger.finished.emit()
    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doShimmedScans(self):
        """ Perform another set of scans now that it is shimmed """
        if not self.setAllCurrentsMarker.isChecked():
                msg = createMessageBox("Note: Shim Process Not Performed",
                                       "If you want correct shims, click above buttons and redo.", "")
                msg.exec() 
                return
        trigger = Trigger()
        trigger.finished.connect(self.updateShimImageAndStats)
        kickoff_thread(self.waitShimmedScans, args=(trigger,))
        self.queueBasisPairScan()


    @disableSlowButtonsTillDone
    def waitdoEvalAppliedShims(self, trigger):
        self.shimInstance.shimZero()
        self.exsiInstance.images_ready_event.clear()
        num_scans = (self.shimInstance.numLoops + (4 if self.withLinGradMarker.isChecked() else 0)) * 2
        if self.countScansCompleted(num_scans):
            self.log("DEBUG: just finished all the shim eval scans")
            self.evaluateAppliedShims()
            self.triggerComputeShimCurrents()
            self.setShimImages()
        else:
            self.log("Error: Scans didn't complete")
            self.exsiInstance.images_ready_event.clear()
            self.exsiInstance.ready_event.clear()
        trigger.finished.emit()
    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doEvalAppliedShims(self):
        """Scan with supposed set shims and evaluate how far from expected they are."""
        trigger = Trigger()
        trigger.finished.connect(self.updateShimImageAndStats)
        kickoff_thread(self.waitdoEvalAppliedShims, args=(trigger,))
        def queueAll():
            # perform the calibration scans for the linear gradients
            self.exsiInstance.sendSetCenterFrequency(int(self.exsiInstance.ogCenterFreq) + int(round(self.currents[self.shimSliceIndex][0])))
            self.queueBasisPairScan()
            if self.withLinGradMarker.isChecked():
                self.queueBasisPairScan([int(round(self.currents[self.shimSliceIndex][1])), 0, 0])
                self.queueBasisPairScan([0, int(round(self.currents[self.shimSliceIndex][1])), 0])
                self.queueBasisPairScan([0, 0, int(round(self.currents[self.shimSliceIndex][1]))])
            for i in range(self.shimInstance.numLoops):
                self.exsiInstance.sendLoadProtocol(f"ConformalShimCalibration3 | {i} {self.currents[self.shimSliceIndex][i+4]:.3f}")
                self.queueBasisPairScanDetails()
        kickoff_thread(queueAll)


    @disableSlowButtonsTillDone
    def waitdoAllShimmedScans(self, trigger, start, numindex):
        self.doAllShimmedScansMarker.setChecked(False)
        self.exsiInstance.images_ready_event.clear()

        for idx in range(start, start+numindex):
            self.log(f"-------------------------------------------------------------")
            self.log(f"DEBUG: STARTING B0MAP {idx-start+1} / {numindex}; slice {idx}")
            self.log(f"DEBUG: currents for this slice are {self.currents[idx]}")
            # setting center frequency
            self.log(f"DEBUG: Setting center frequency from {self.exsiInstance.ogCenterFreq} to {int(self.exsiInstance.ogCenterFreq) + int(round(self.currents[idx][0]))}")
            self.exsiInstance.sendSetCenterFrequency(int(self.exsiInstance.ogCenterFreq) + int(round(self.currents[idx][0])))

            # setting the linear shims
            if self.withLinGradMarker.isChecked():
                linGrads = self.linShimFactor * self.currents[idx][1:4]
                linGrads = np.round(linGrads).astype(int)
                self.log(f"DEBUG: Setting linear gradients to {linGrads}")
                self.exsiInstance.sendSetShimValues(*linGrads)

            # setting the loop shim currents
            for i in range(self.shimInstance.numLoops):
                if self.withLinGradMarker.isChecked():
                    self.log(f"DEBUG: Setting currents for loop {i} to {self.currents[idx][i+4]:.3f}, multiplied by {self.shimCalCurrent}")
                    self.shimSetCurrentManual(i%8, self.shimCalCurrent * self.currents[idx][i+4], i//8)
                else:
                    self.log(f"DEBUG: Setting currents for loop {i} to {self.currents[idx][i+1]:.3f}, multiplied by {self.shimCalCurrent}")
                    self.shimSetCurrentManual(i%8, self.shimCalCurrent * self.currents[idx][i+1], i//8)

            self.log(f"DEBUG: now waiting to actually perform the slice")
            if self.countScansCompleted(2):
                # perform the rest of these functions in another thread so that the shim setting doesn't lag behind too much
                def updateVals():
                    self.computeShimmedB0Map(idx)
                    self.evaluateShimImages()
                    self.setShimImages()
                    self.validateShimSliceIndexControls(self.shimSliceIndex)
                    trigger.finished.emit()
                kickoff_thread(updateVals)
            else:
                self.log("Error: Scans didn't complete")
                self.exsiInstance.images_ready_event.clear()
                self.exsiInstance.ready_event.clear()
    @requireExsiConnection
    @requireShimConnection
    @requireAssetCalibration
    def doAllShimmedScans(self):
        if not self.currentsComputedMarker.isChecked():
            return

        # compute how many scans needed, i.e. how many slices are not Nans out of the ROI
        startindex = None
        numindexes = 0
        for i in range(self.shimData[0].shape[1]):
            if startindex is None and self.currents[i] is not None:
                startindex = i
            if self.currents[i] is not None:
                numindexes += 1
        startindex += 1
        numindexes -= 2 # chop off the first and last index
        self.log(f"DEBUG: Starting at index {startindex} and doing {numindexes} B0MAPS")

        trigger = Trigger()
        trigger.finished.connect(self.updateShimImageAndStats)
        kickoff_thread(self.waitdoAllShimmedScans, args=(trigger,startindex, numindexes))
        
        def queueAll():
            for i in range(startindex, startindex + numindexes):
                self.queueBasisPairScan()
        kickoff_thread(queueAll)
        
    ##### SHIM COMPUTATION FUNCTIONS #####   

    def computeMask(self):
        """compute the mask for the shim images."""
        self.finalMask = []
        for i in range(self.shimData[0].shape[1]):
            self.finalMask.append(createMask(self.shimData[0], self.basisB0maps, roi=self.roiMask, sliceIndex=i))
        
    
    def evaluateShimImages(self):
        """evaluate the shim images and store the stats in the stats array."""
        for i in range(3):
            if self.shimStatStrs[i] is None and self.shimData[i] is not None:
                self.shimStatStrs[i] = [None for _ in range(self.shimData[0].shape[1])]
                self.shimStats[i]  = [None for _ in range(self.shimData[0].shape[1])]
            if self.shimData[i] is not None:
                for j in range(self.shimData[0].shape[1]):
                    if i == 0:
                        statsstr, stats = evaluate(self.shimData[i][self.finalMask[j]], self.debugging)
                        self.shimStatStrs[i][j] = statsstr
                        self.shimStats[i][j] = stats
                    elif self.shimData[i][j] is not None:
                        statsstr, stats =  evaluate(self.shimData[i][j][self.finalMask[j]], self.debugging)
                        self.shimStatStrs[i][j] = statsstr
                        self.shimStats[i][j] = stats
    
    def evaluateAppliedShims(self):
        b0maps = compute_b0maps(self.shimInstance.numLoops + 4, self.localExamRootDir)
        for i in range(len(b0maps)):
            #save actual b0map to an eval folder, within which it says the current slice that we are on...
            # save the b0map to the eval folder
            evalDir = os.path.join(self.config['rootDir'], "results", self.exsiInstance.examNumber, "eval", f"slice{self.shimSliceIndex}")
            if not os.path.exists(evalDir):
                os.makedirs(evalDir)
            np.save(os.path.join(evalDir, f"b0map{i}.npy"), b0maps[i])
            # compute the difference from the expected b0map
            expected = self.shimData[0]
            if i == 0:
                expected += self.currents[self.shimSliceIndex][i] * np.ones(self.shimData[0].shape)
            else:
                expected += self.currents[self.shimSliceIndex][i] * self.basisB0maps[i-1]

            np.save(os.path.join(evalDir, f"expected{i}.npy"), expected)

            difference = b0maps[i] - expected
            fig, ax = plt.subplots(figsize=(8, 6))
            im = ax.imshow(difference[:,self.shimSliceIndex,:], cmap='jet', vmin=-100, vmax=100)
            cbar = plt.colorbar(im)

            plt.title(f"difference basis{i}, slice{self.shimSliceIndex}", size=10)
            plt.axis('off')
            
            fig.savefig(os.path.join(evalDir, f"difference{i}.png"), bbox_inches='tight', transparent=False)
            plt.close(fig)



    def computeBackgroundB0map(self):
        # assumes that you have just gotten background by queueBasisPairScan
        b0maps = compute_b0maps(1, self.localExamRootDir)
        self.backgroundDCMdir = listSubDirs(self.localExamRootDir)[-1]
        self.shimData[0] = b0maps[0]

    def computeBasisB0maps(self):
        # assumes that you have just gotten background by queueBasisPairScan
        if self.withLinGradMarker.isChecked():
            self.rawBasisB0maps = compute_b0maps(self.shimInstance.numLoops + 3, self.localExamRootDir)
        else:
            self.rawBasisB0maps = compute_b0maps(self.shimInstance.numLoops, self.localExamRootDir)
    
    def computeShimCurrents(self):
        # run whenever both backgroundB0Map and basisB0maps are computed or if one new one is obtained
        self.basisB0maps = subtractBackground(self.shimData[0], self.rawBasisB0maps)
        self.basisImagesSet = False # make sure to rerender the basis images when switching to the basis images tab
        self.computeMask()

        self.currents = [None for _ in range(self.shimData[0].shape[1])]
        for i in range(self.shimData[0].shape[1]):
            # want to include slice in front and behind in the mask when solving currents though:
            mask = self.finalMask[i]
            if i > 0:
                mask = np.logical_or(mask, self.finalMask[i-1])
            if i < self.shimData[0].shape[1] - 1:
                mask = np.logical_or(mask, self.finalMask[i+1])
            #NOTE: the first and last current that is solved will be for an empty slice...
            self.currents[i] = solveCurrents(self.shimData[0], self.basisB0maps, mask, withLinGrad=self.withLinGradMarker.isChecked(), debug=self.debugging)

        # if not all currents are none
        if not all([c is None for c in self.currents]):
            self.shimData[1] = [self.shimData[0].copy() for _ in range(self.shimData[0].shape[1])]
            # self.log(f"DEBUG: reset shimdata[1] to be list of shimdata[0]s, shape: {self.shimData[1][0].shape}")
            for i in range(self.shimData[0].shape[1]):
                # self.log(f"DEBUG: checking currents for shimData[1][{i}]")
                if self.currents[i] is not None:
                    # self.log(f"DEBUG: adding center freq offset to shimData[1][{i}]")
                    self.shimData[1][i] += self.currents[i][0] * np.ones(self.shimData[0].shape)
                    numIter = self.shimInstance.numLoops
                    if self.withLinGradMarker.isChecked():
                        numIter += 3
                    for j in range(numIter):
                        # self.log(f"DEBUG: adding current {j} to shimData[1][{i}]")
                        self.shimData[1][i] += self.currents[i][j+1] * self.basisB0maps[j]
                else:
                    self.shimData[1][i] = None
            self.currentsComputedMarker.setChecked(True)
        else:
            # TODO: can't make a message box in another thread i think
            # make a message box saying currents could not compute
            # msg = createMessageBox("Error: Could Not Solve For Currents",
            #                        "Might be that they aren't getting set and least squares becomes lowrank.", "")
            # msg.exec()
            self.log("Error: Could not solve for currents. Look at error hopefully in output")

    def computeShimmedB0Map(self, idx = None):
        b0maps = compute_b0maps(1, self.localExamRootDir)
        if self.shimData[2] is None:
            self.shimData[2] = [None for _ in range(self.shimData[0].shape[1])]
        if idx is not None:
            self.shimData[2][idx] = b0maps[0]
        else:
            self.shimData[2][self.shimSliceIndex] = b0maps[0]

    ##### SCAN DATA RELATED FUNCTIONS #####   

    # TODO: move most of these transfer functions into their own UTIL file. dataUtils.py or smth
    def execSSHCommand(self, command):
        # Initialize the SSH client
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # Automatically add host key
        try:
            client.connect(hostname=self.config['host'], port=self.config['hvPort'], username=self.config['hvUser'], password=self.config['hvPassword'])
            stdin, stdout, stderr = client.exec_command(command)
            return stdout.readlines()  # Read the output of the command

        except Exception as e:
            self.log(f"Connection or command execution failed: {e}")
        finally:
            client.close()

    def execRsyncCommand(self, source, destination):
        # Construct the SCP command using sshpass
        cmd = f"sshpass -p {self.config['hvPassword']} rsync -avz {self.config['hvUser']}@{self.config['host']}:{source} {destination}"

        # Execute the SCP command
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Wait for the command to complete
        stdout, stderr = process.communicate()
        
        # Check if the command was executed successfully
        if process.returncode == 0:
            return stdout.decode('utf-8')
        else:
            return f"Error: {stderr.decode('utf-8')}"

    def getLastSuccessgradients(self):
        # Command to extract the last successful setting of the shim currents
        self.log(f"Debug: attempting to find the last used gradients")
        command = "tail -n 100 /usr/g/service/log/Gradient.log | grep 'Prescn Success: AS Success' | tail -n 1"
        output = self.execSSHCommand(command)
        if output:
            last_line = output[0].strip()
            # Use regex to find X, Y, Z values
            match = re.search(r'X =\s+(-?\d+)\s+Y =\s+(-?\d+)\s+Z =\s+(-?\d+)', last_line)
            if match:
                gradients = [int(match.group(i)) for i in range(1, 4)]
                self.log(f"Debug: found that linear shims got set to {gradients}")
                self.linShims = gradients
                return True
            self.log(f"DEBUG: no matches!")
        self.log(f"Debug: failed to find the last used gradients")
        return False
            
    def setGehcExamDataPath(self):
        if self.exsiInstance.examNumber is None:
            self.log("Error: No exam number found in the exsi client instance.")
            return
        exam_number = self.exsiInstance.examNumber
        output = self.execSSHCommand("pathExtract "+exam_number)
        if output:
            last_line = output[-1].strip() 
        else:
            return
        parts = last_line.split("/")
        self.gehcExamDataPath = os.path.join("/", *parts[:7])
        self.log(f"Debug: obtained exam data path: {self.gehcExamDataPath}")

    def transferScanData(self):
        self.log(f"Debug: initiating transfer using rsync.")
        if self.gehcExamDataPath is None:
            self.setGehcExamDataPath()
        self.execRsyncCommand(self.gehcExamDataPath + '/*', self.localExamRootDir)

    def getLatestData(self, stride=1, offset=0):
        latestDCMDir = listSubDirs(self.localExamRootDir)[-1]
        res = extractBasicImageData(latestDCMDir, stride, offset)
        self.currentROIImageData, self.currentImageTE, self.currentImageOrientation = res
    
    def getROIBackgound(self):
        self.log('Debug: extracting the background mag image')
        res = extractBasicImageData(self.backgroundDCMdir, stride=3, offset=0)
        self.log('Debug: done extracting the background mag image')
        self.currentROIImageData = res[0]


    def saveResults(self):
        def helper():
            if np.array([d is None for d in self.shimData]).all():
                return
            self.log("Debug: saving Images")

            # get the time and date
            dt = datetime.now()
            dt = dt.strftime("%Y%m%d_%H%M%S")

            self.resultsDir = os.path.join(self.config['rootDir'], "results", self.exsiInstance.examNumber, dt)
            if not os.path.exists(self.resultsDir):
                os.makedirs(self.resultsDir)
            self.log(f"Debug: saving results to {self.resultsDir}")
            
            # need to crop and then save the image using the saveImage function from 
            data = [None, None, None]
            labels = ["Background", "Expected", "Shimmed"]
            for i in range(3):
                if self.shimData[i] is not None:
                    # make nones for every slice
                    data[i] = np.where(np.zeros_like(self.shimData[0], dtype=bool), np.nan, np.nan).transpose(1,0,2)
                    # crop with the correct mask 
                    for j in range(self.shimData[0].shape[i]):
                        if i == 0:
                            if self.finalMask is not None and self.finalMask[j] is not None:
                                data[i][j] = np.where(self.finalMask[j], self.shimData[i], np.nan)[:,j,:]
                        else:
                            if self.shimData[i][j] is not None and self.finalMask is not None and self.finalMask[j] is not None:
                                data[i][j] = np.where(self.finalMask[j], self.shimData[i][j], np.nan)[:,j,:]
            
            bases = [None for _ in range(self.shimInstance.numLoops+3)]
            for i in range(self.shimInstance.numLoops+3):
                if self.basisB0maps[i] is not None:
                    bases[i] = np.where(np.zeros_like(self.basisB0maps[0], dtype=bool), np.nan, np.nan).transpose(1,0,2)
                    for j in range(self.basisB0maps[0].shape[1]):
                        if self.finalMask[j] is not None:
                            bases[i][j] = np.where(self.finalMask[j], self.basisB0maps[i], np.nan)[:,j,:]


            vmax = -np.inf
            for i in range(len(data)):
                if data[i] is not None:
                    vmax = np.nanmax([np.nanmax(np.abs(data[i])), vmax])
            for i in range(len(bases)):
                if bases[i] is not None:
                    vmax = np.nanmax([np.nanmax(np.abs(bases[i])), vmax])
            print(f"vmax: {vmax}")

            bases = np.array(bases)
            
            # save individual images and stats

            for i in range(3):
                if data[i] is not None:

                    imageTypeSaveDir = os.path.join(self.resultsDir, labels[i])
                    imagesDir = os.path.join(imageTypeSaveDir, 'images')
                    histDir = os.path.join(imageTypeSaveDir, 'histograms')
                    for d in [imageTypeSaveDir, imagesDir, histDir]:
                        if not os.path.exists(d):
                            os.makedirs(d)

                    self.log(f"Debug: saving slice images and histograms for {labels[i]}")
                    for j in range(self.shimData[0].shape[1]):
                        # save a perslice B0Map image and histogram
                        saveImage(imagesDir, labels[i], data[i][j], j, vmax)
                        saveHistogram(histDir, labels[i], data[i][j], j)
                    
                    self.log(f"Debug: saving stats for {labels[i]}")
                    # save all the slicewise stats, appended into one file
                    saveStats(imageTypeSaveDir, labels[i], self.shimStatStrs[i])
                    # generate and then save volume wise stats
                    data[i] = np.array(data[i])
                    stats, statarr = evaluate(data[i].flatten(), self.debugging)
                    saveStats(imageTypeSaveDir, labels[i], stats, volume=True)

                    self.log(f"Debug: saving volume stats for {labels[i]}")
                    # save volume wise histogram 
                    saveHistogram(imageTypeSaveDir, labels[i], data[i], -1)
            
            for i in range(bases.shape[0]):
                if bases[i] is not None:
                    basesDir = os.path.join(self.resultsDir, "basisMaps")
                    baseDir = os.path.join(basesDir, f"basis{i}")
                    for d in [basesDir, baseDir]:
                        if not os.path.exists(d):
                            os.makedirs(d)
                    for j in range(bases.shape[1]):
                        saveImage(baseDir, f"basis{i}", bases[i][j], j, vmax)
            
            # save the histogram  all images overlayed
            if data[0] is not None and data[1] is not None:
                self.log(f"Debug: saving overlayed volume stats for ROI")
                data = np.array(data) # convert to numpy array
                # for the volume entirely
                saveHistogramsOverlayed(self.resultsDir, labels, data, -1)
                # for each slice independently
                overlayHistogramDir = os.path.join(self.resultsDir, 'overlayedHistogramPerSlice')
                if not os.path.exists(overlayHistogramDir):
                    os.makedirs(overlayHistogramDir)
                for j in range(self.shimData[0].shape[1]):
                    saveHistogramsOverlayed(overlayHistogramDir, labels, data[:,j], j)
            
            # save the numpy data
            np.save(os.path.join(self.resultsDir, 'shimData.npy'), data)
            np.save(os.path.join(self.resultsDir, 'shimStats.npy'), self.shimStats)
            np.save(os.path.join(self.resultsDir, 'basis.npy'), bases)
            self.log(f"Debug: done saving results to {self.resultsDir}")
        kickoff_thread(helper)


    ##### OTHER METHODS ######

    # TODO: remove these because they seem useless
    def execBashCommand(self, cmd):
        # Execute the bash command
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Wait for the command to complete
        stdout, stderr = process.communicate()
        
        # Check if the command was executed successfully
        if process.returncode == 0:
            return stdout.decode('utf-8')
        else:
            return f"Error: {stderr.decode('utf-8')}"

    def execSCPCommand(self, source, destination):
        # Construct the SCP command using sshpass
        cmd = f"sshpass -p {self.config['hvPassword']} scp -r {self.config['hvUser']}@{self.config['host']}:{source} {destination}"

        # Execute the SCP command
        process = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Wait for the command to complete
        stdout, stderr = process.communicate()

        # Check if the command was executed successfully
        if process.returncode == 0:
            return stdout.decode('utf-8')
        else:
            return f"Error: {stderr.decode('utf-8')}"

    def log(self, msg, forceStdOut=False):
        # record a timestamp and prepend to the message
        current_time = datetime.now()
        formatted_time = current_time.strftime('%H:%M:%S')
        msg = f"{formatted_time} {msg}"
        # only print if in debugging mode, or if forceStdOut is set to True
        if self.debugging or forceStdOut:
            print(msg)
        # always write to the log file
        with open(self.guiLog, 'a') as file:
            file.write(f"{msg}\n")

    def closeEvent(self, event):
        self.log("INFO: Starting to close", True)
        if self.exsiLogMonitorThread:
            self.exsiLogMonitorThread.stop()
            self.exsiLogMonitorThread.wait()
        if self.shimLogMonitorThread:
            self.shimLogMonitorThread.stop()
            self.shimLogMonitorThread.wait()
        self.log("INFO: Done with logmonitorthread", True)
        if self.exsiInstance:
            self.log("INFO: Stopping exsi client instance", True)
            self.exsiInstance.stop()
        if self.shimInstance:
            self.log("INFO: Stopping shim client instance", True)
            self.shimInstance.stop()
        self.log("INFO: Done with exsi instance", True)
        event.accept()
        super().closeEvent(event)

def handle_exit(signal_received, frame):
    # Handle any cleanup here
    print('SIGINT or CTRL-C detected. Exiting gracefully.')
    QApplication.quit()

def load_config(filename):
    with open(filename, 'r') as file:
        return json.load(file)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)
    
    # try:
    config = load_config('config.json')
    app = QApplication(sys.argv)
    ex = ExsiGui(config)
    ex.show()
    sys.exit(app.exec())
