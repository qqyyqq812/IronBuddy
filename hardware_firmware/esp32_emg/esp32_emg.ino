#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// -------------------------------------------------------------------
// 节点 1-3 (广播成型): 硬件底座与 BLE 数据泵
// 铁律：只能使用 ADC1 (GPIO 32~39)
// -------------------------------------------------------------------

#define EMG_CH1_PIN 32
#define EMG_CH2_PIN 33

// BLE UUID 契约 (Sync Point A 约定)
#define SERVICE_UUID        "19B10000-E8F2-537E-4F6C-D104768A1214"
#define CHARACTERISTIC_UUID "19B10001-E8F2-537E-4F6C-D104768A1214"

// FreeRTOS 任务句柄
TaskHandle_t ADCTaskHandle = NULL;

// BLE 状态
BLEServer* pServer = NULL;
BLECharacteristic* pCharacteristic = NULL;
bool deviceConnected = false;
bool oldDeviceConnected = false;

// 数据包约定结构 (44 Bytes, 强制紧凑排列防内存对齐补齐)
struct __attribute__((packed)) EMGPayload {
    uint16_t ch1[10];
    uint16_t ch2[10];
    uint32_t timestamp;
};

// BLE 连接回调状态机
class MyServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      deviceConnected = true;
      Serial.println("[BLE] Client Connected!");
    };

    void onDisconnect(BLEServer* pServer) {
      deviceConnected = false;
      Serial.println("[BLE] Client Disconnected. Waiting for reconnect...");
    }
};

// 肌电数据连续采集任务 (1kHz 物理采样，100Hz 射频打包)
void vADCSamplingTask(void *pvParameters) {
    (void)pvParameters;

    const TickType_t xFrequency = pdMS_TO_TICKS(1); 
    TickType_t xLastWakeTime = xTaskGetTickCount();
    
    EMGPayload payload;
    uint8_t sampleIndex = 0;

    for (;;) {
        // 读取 ADC1 通道
        payload.ch1[sampleIndex] = analogRead(EMG_CH1_PIN);
        payload.ch2[sampleIndex] = analogRead(EMG_CH2_PIN);
        sampleIndex++;

        // 当积攒满 10 次采样 (相当于经过了 10ms)
        if (sampleIndex >= 10) {
            payload.timestamp = millis();

            // 若蓝牙已连入，则扣动扳机发送 Notify 波
            if (deviceConnected) {
                pCharacteristic->setValue((uint8_t*)&payload, sizeof(payload));
                pCharacteristic->notify();
            }

            sampleIndex = 0; // 单包打完，清空计数器重拾
        }
        
        // 维持严格的 1kHz
        vTaskDelayUntil(&xLastWakeTime, xFrequency);
    }
}

void setup() {
    Serial.begin(115200);
    delay(500);
    Serial.println("[Worker_1 Boot] ESP32-sEMG BLE Service Starting...");
    
    analogReadResolution(12);

    // ================== 初始化 BLE GATT ==================
    BLEDevice::init("IronBuddy_EMG_Pod");
    pServer = BLEDevice::createServer();
    pServer->setCallbacks(new MyServerCallbacks());

    BLEService *pService = pServer->createService(SERVICE_UUID);

    // 设置 Notify 属性，并加装 2902 描述符让客户端可订阅
    pCharacteristic = pService->createCharacteristic(
                        CHARACTERISTIC_UUID,
                        BLECharacteristic::PROPERTY_NOTIFY
                      );
    pCharacteristic->addDescriptor(new BLE2902());

    pService->start();

    BLEAdvertising *pAdvertising = BLEDevice::getAdvertising();
    pAdvertising->addServiceUUID(SERVICE_UUID);
    pAdvertising->setScanResponse(false);
    pAdvertising->setMinPreferred(0x0);  // 帮助 iOS 端降低扫描难度
    BLEDevice::startAdvertising();
    Serial.println("[BLE] Server Setup Done! Advertising...");
    // =====================================================

    // 独立拉起 ADC 后台管道 (挂载 Core 1)
    xTaskCreatePinnedToCore(
        vADCSamplingTask,   
        "ADC_1kHz_Tsk",     
        4096,               // BLE 发送有内存开销，稳妥调大到 4K 字
        NULL,               
        configMAX_PRIORITIES - 1,  
        &ADCTaskHandle,     
        1                   
    );
}

void loop() {
    // 处理蓝牙断连的重新广播恢复状态机
    if (!deviceConnected && oldDeviceConnected) {
        delay(500); // 留出一点缓时给蓝牙栈
        pServer->startAdvertising(); 
        Serial.println("[BLE] Restarting Advertising...");
        oldDeviceConnected = deviceConnected;
    }
    
    if (deviceConnected && !oldDeviceConnected) {
        oldDeviceConnected = deviceConnected;
    }

    // 主线程低功耗摸鱼
    vTaskDelay(pdMS_TO_TICKS(100));
}
