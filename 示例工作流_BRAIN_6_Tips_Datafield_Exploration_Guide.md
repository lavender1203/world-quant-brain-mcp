# BRAIN TIPS: 6 Ways to Quickly Evaluate a New Dataset
## WorldQuant BRAIN Platform - Datafield Exploration Guide

**Original Post**: [BRAIN TIPS] 6 ways to quickly evaluate a new dataset  
**Author**: KA64574  
**Date**: 2 years ago  
**Followers**: 265 people  

---

## 🎯 **Overview**

WorldQuant BRAIN has thousands of datafields for you to create alphas. But how do you quickly understand a new datafield? Here are 6 proven methods to evaluate and understand new datasets efficiently.

**Important**: Simulate the below expressions in **"None" neutralization** and **decay 0 setting** and **test_period P0Y0M**. Obtain insights of specific parameters using the **Long Count** and **Short Count** in the **IS Summary section** of the results.
**Watch Out**: - Data type (matrix/vector), please note, these are two special definition here and not similar as we knew in math. Different data types have different characteristics and usage rule; if it is a matrix data type, you can use the datafield directly, but if it is a vector data type, you should use a vector operator to convert the datafield to a matrix data type. Thus, for a vector data type, you should find proper vector operator via mcp then put it into the following test.

---

## 📊 **The 6 Exploration Methods**

### **1. Basic Coverage Analysis**
**Expression**: `datafield`, for vector data type, the expression should be `vector_operator(datafield)`, please note, the vector_operator is the operator that you found via mcp.
**Insight**: % coverage, would approximately be ratio of (Long Count + Short Count in the IS Summary) / (Universe Size in the settings)

**Purpose**: Understand the basic availability of data across the universe
**What it tells you**: How many instruments have data for this field on average

---

### **2. Non-Zero Value Coverage**
**Expression**: `datafield != 0 ? 1 : 0` , for vector data type, the expression should be `vector_operator(datafield) != 0 ? 1 : 0`, please note, the vector_operator is the operator that you found via mcp.
**Insight**: Coverage. Long Count indicates average non-zero values on a daily basis

**Purpose**: Distinguish between missing data and actual zero values
**What it tells you**: Whether the field has meaningful data vs. just coverage gaps

---

### **3. Data Update Frequency Analysis**
**Expression**: `ts_std_dev(datafield,N) != 0 ? 1 : 0` , for vector data type, the expression should be `ts_std_dev(vector_operator(datafield),N) != 0 ? 1 : 0`, please note, the vector_operator is the operator that you found via mcp.
**Insight**: Frequency of unique data (daily, weekly, monthly etc.)

**Key Points**:
- Some datasets have data backfilled for missing values, while some do not
- This expression can be used to find the frequency of unique datafield updates by varying N (no. of days)
- Datafields with quarterly unique data frequency would see a Long Count + Short Count value close to its actual coverage when N = 66 (quarter)
- When N = 22 (month) Long Count + Short Count would be lower (approx. 1/3rd of coverage)
- When N = 5 (week), Long Count + Short Count would be even lower

**Purpose**: Understand how often the data actually changes vs. being backfilled
**What it tells you**: Data freshness and update patterns

---

### **4. Data Bounds Analysis**
**Expression**: `abs(datafield) > X`  , for vector data type, the expression should be `abs(vector_operator(datafield)) > X`, please note, the vector_operator is the operator that you found via mcp.
**Insight**: Bounds of the datafield. Vary the values of X and see the Long Count

**Example**: X=1 will indicate if the field is normalized to values between -1 and +1

**Purpose**: Understand the range and scale of the data values
**What it tells you**: Whether data is normalized, what the typical value ranges are

---

### **5. Central Tendency Analysis**
**Expression**: `ts_median(datafield, 1000) > X`  , for vector data type, the expression should be `ts_median(vector_operator(datafield), 1000) > X`, please note, the vector_operator is the operator that you found via mcp.
**Insight**: Median of the datafield over 5 years. Vary the values of X and see the Long Count

**Note**: Similar process can be applied to check the mean of the datafield

**Purpose**: Understand the typical values and central tendency of the data
**What it tells you**: Whether the data is skewed, what typical values look like

---

### **6. Data Distribution Analysis**
**Expression**: `X < scale_down(datafield) && scale_down(datafield) < Y`  , for vector data type, the expression should be `X < scale_down(vector_operator(datafield)) && scale_down(vector_operator(datafield)) < Y`, please note, the vector_operator is the operator that you found via mcp.
**Insight**: Distribution of the datafield

**Key Points**:
- `scale_down` acts as a MinMaxScaler that can preserve the original distribution of the data
- X and Y are values that vary between 0 and 1 that allow us to check how the datafield distributes across its range

**Purpose**: Understand how data is distributed across its range
**What it tells you**: Whether data is evenly distributed, clustered, or has specific patterns

---

## 🔍 **Practical Example**

**Example**: If you simulate `[close <= 0]`, you will see Long and Short Counts as 0. This implies that closing price always has a positive value (as expected!)

**What this demonstrates**: The validation that your understanding of the data is correct

---

## 📋 **Implementation Workflow**

### **Step 1: Setup**
1. Set neutralization to "None"
2. Set decay to 0
3. Choose appropriate universe and time period

### **Step 2: Run Basic Tests**
1. Start with expression 1 (`datafield`) to get baseline coverage
2. Run expression 2 (`datafield != 0 ? 1 : 0`) to understand non-zero coverage

### **Step 3: Analyze Update Frequency**
1. Test with N = 5 (weekly)
2. Test with N = 22 (monthly) 
3. Test with N = 66 (quarterly)
4. Compare results to understand update patterns

### **Step 4: Explore Value Ranges**
1. Test various thresholds for bounds analysis
2. Test various thresholds for central tendency
3. Test various ranges for distribution analysis

### **Step 5: Document Insights**
1. Record Long Count and Short Count for each test
2. Calculate coverage ratios
3. Note patterns in update frequency
4. Document value ranges and distributions

---

## 🎯 **When to Use Each Method**

| Method | Best For | When to Use |
|--------|----------|-------------|
| **1. Basic Coverage** | Initial assessment | First exploration of any new field |
| **2. Non-Zero Coverage** | Data quality check | After basic coverage to understand meaningful data |
| **3. Update Frequency** | Data freshness | When you need to understand how often data changes |
| **4. Data Bounds** | Value ranges | When you need to understand data scale and normalization |
| **5. Central Tendency** | Typical values | When you need to understand what "normal" looks like |
| **6. Distribution** | Data patterns | When you need to understand how data is spread |

---

## ⚠️ **Important Considerations**

### **Neutralization Setting**
- **Use "None"** for these exploration tests
- This ensures you're seeing the raw data behavior
- Other neutralization settings may mask important patterns

### **Decay Setting**
- **Use 0** for these exploration tests
- This ensures you're seeing the actual data values
- Decay can smooth out important variations

### **Universe Selection**
- Choose a universe that represents your target use case
- Consider both coverage and representativeness
- Large universes may have different patterns than smaller ones

### **Time Period**
- Use sufficient history to see patterns
- Consider seasonal or cyclical effects
- Ensure you have enough data for statistical significance

---

## 🚀 **Advanced Applications**

### **Combining Methods**
- Use multiple methods together for comprehensive understanding
- Cross-reference results to validate insights
- Look for inconsistencies that might indicate data quality issues

### **Custom Variations**
- Modify expressions to test specific hypotheses
- Combine with other operators for deeper insights
- Create custom metrics based on your findings

### **Automation**
- These tests can be automated for systematic dataset evaluation
- Create standardized evaluation reports
- Track changes in data quality over time

---

## 📚 **Related Resources**

- **BRAIN Platform Documentation**: Understanding Data concepts
- **Data Explorer Tool**: Visual exploration of data fields
- **Simulation Results**: Detailed analysis of field behavior
- **Community Forums**: User experiences and best practices

---

*This guide provides a systematic approach to understanding new datafields on the WorldQuant BRAIN platform. Use these methods to quickly assess data quality, coverage, and characteristics before incorporating fields into your alpha strategies.*
